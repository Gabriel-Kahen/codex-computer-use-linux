use crate::coordination_protocol::{
    DesktopBackend, IdentityAttribute, ProcessIdentity, SessionIdentity, WindowIdentity,
};
use crate::windowing::backends::{i3, kwin};
use crate::windowing::registry::{
    self, WindowListPolicy, COSMIC_WAYLAND_BACKEND, GNOME_SHELL_EXTENSION_BACKEND,
    GNOME_SHELL_INTROSPECT_BACKEND, HYPRLAND_BACKEND, I3_BACKEND, KWIN_BACKEND, NIRI_BACKEND,
    X11_BACKEND,
};
use crate::windowing::types::WindowInfo;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use zbus::{Connection, Proxy};

#[derive(Clone)]
pub(crate) struct CoordinationScope {
    pub(crate) state_dir: PathBuf,
    pub(crate) session: SessionIdentity,
    pub(crate) window: Option<WindowIdentity>,
    pub(crate) legacy_hyprland_binding: Option<BTreeMap<String, serde_json::Value>>,
}

pub(crate) fn state_dir() -> Option<PathBuf> {
    env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .map(|root| root.join("same-session-computer-use"))
}

pub(crate) async fn resolve(window_id: Option<u64>) -> Result<CoordinationScope, String> {
    crate::diagnostics::hydrate_session_bus_env();
    let state_dir = state_dir().ok_or("cannot locate the coordination state directory")?;
    let windows = registry::list_windows_with_policy(WindowListPolicy::Fresh)
        .await
        .map_err(|error| format!("cannot identify the active desktop session: {error:#}"))?;
    let selected = match window_id {
        Some(id) => Some(
            windows
                .iter()
                .find(|window| window.window_id == id)
                .ok_or_else(|| format!("window_id {id} is not present in the fresh window list"))?,
        ),
        None => windows.first(),
    };
    let selected = selected.ok_or("cannot identify a desktop session without any windows")?;
    let backend = desktop_backend(&selected.backend)?;
    let uid = fs::metadata("/proc/self")
        .map_err(|error| format!("cannot identify the current uid: {error}"))?
        .uid();
    let attributes = session_attributes(backend).await?;
    let session = SessionIdentity {
        backend,
        uid,
        attributes,
    };
    let window = match window_id {
        Some(_) => Some(window_identity(selected, backend).await?),
        None => None,
    };
    let legacy_hyprland_binding = (backend == DesktopBackend::Hyprland)
        .then(|| legacy_hyprland_binding(uid))
        .transpose()?;
    Ok(CoordinationScope {
        state_dir,
        session,
        window,
        legacy_hyprland_binding,
    })
}

fn desktop_backend(value: &str) -> Result<DesktopBackend, String> {
    match value {
        COSMIC_WAYLAND_BACKEND => Ok(DesktopBackend::Cosmic),
        GNOME_SHELL_EXTENSION_BACKEND | GNOME_SHELL_INTROSPECT_BACKEND => Ok(DesktopBackend::Gnome),
        HYPRLAND_BACKEND => Ok(DesktopBackend::Hyprland),
        I3_BACKEND => Ok(DesktopBackend::I3),
        NIRI_BACKEND => Ok(DesktopBackend::Niri),
        KWIN_BACKEND => Ok(DesktopBackend::Plasma),
        X11_BACKEND => Ok(DesktopBackend::X11),
        _ => Err(format!("unsupported coordination backend {value:?}")),
    }
}

async fn session_attributes(
    backend: DesktopBackend,
) -> Result<BTreeMap<String, IdentityAttribute>, String> {
    let mut attributes = BTreeMap::new();
    match backend {
        DesktopBackend::Hyprland => {
            insert_env(
                &mut attributes,
                "hyprland_instance",
                "HYPRLAND_INSTANCE_SIGNATURE",
            )?;
            insert_env(&mut attributes, "wayland_display", "WAYLAND_DISPLAY")?;
        }
        DesktopBackend::Niri => insert_socket(
            &mut attributes,
            "niri_socket",
            PathBuf::from(required_env("NIRI_SOCKET")?).as_path(),
        )?,
        DesktopBackend::I3 => insert_socket(
            &mut attributes,
            "i3_socket",
            i3::i3_socket_path()
                .ok_or("cannot locate the active i3 socket")?
                .as_path(),
        )?,
        DesktopBackend::X11 => {
            let display = required_env("DISPLAY")?;
            if !display.starts_with(':') && !display.starts_with("unix:") {
                return Err("remote X11 displays cannot be coordinated safely".to_string());
            }
            let number = display
                .trim_start_matches("unix:")
                .trim_start_matches(':')
                .split('.')
                .next()
                .ok_or("DISPLAY is malformed")?;
            insert_socket(
                &mut attributes,
                "x11_socket",
                &PathBuf::from(format!("/tmp/.X11-unix/X{number}")),
            )?;
        }
        DesktopBackend::Cosmic => {
            let socket = PathBuf::from(required_env("XDG_RUNTIME_DIR")?)
                .join(required_env("WAYLAND_DISPLAY")?);
            insert_socket(&mut attributes, "wayland_socket", &socket)?;
        }
        DesktopBackend::Gnome | DesktopBackend::Plasma => {
            insert_env(&mut attributes, "session_bus", "DBUS_SESSION_BUS_ADDRESS")?;
            let service = if backend == DesktopBackend::Gnome {
                "org.gnome.Shell"
            } else {
                "org.kde.KWin"
            };
            let (bus_id, owner) = service_identity(service).await?;
            attributes.insert("bus_id".to_string(), IdentityAttribute::Text(bus_id));
            attributes.insert(
                "compositor_owner".to_string(),
                IdentityAttribute::Text(owner),
            );
        }
    }
    Ok(attributes)
}

async fn service_identity(service: &str) -> Result<(String, String), String> {
    let connection = Connection::session()
        .await
        .map_err(|error| format!("cannot connect to the session bus: {error}"))?;
    let proxy = Proxy::new(
        &connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
    )
    .await
    .map_err(|error| format!("cannot inspect session-bus ownership: {error}"))?;
    let bus_id = proxy
        .call("GetId", &())
        .await
        .map_err(|error| format!("cannot identify the session bus: {error}"))?;
    let owner = proxy
        .call("GetNameOwner", &(service))
        .await
        .map_err(|error| format!("session bus has no owner for {service}: {error}"))?;
    Ok((bus_id, owner))
}

async fn window_identity(
    window: &WindowInfo,
    backend: DesktopBackend,
) -> Result<WindowIdentity, String> {
    let id = if backend == DesktopBackend::Plasma {
        kwin::kwin_uuid_for_window_id(window.window_id)
            .await
            .map_err(|error| format!("cannot resolve the KWin window UUID: {error:#}"))?
            .ok_or("KWin no longer reports the target window")?
            .to_ascii_lowercase()
    } else {
        format!("0x{:x}", window.window_id)
    };
    let process = if matches!(
        backend,
        DesktopBackend::Hyprland | DesktopBackend::I3 | DesktopBackend::X11
    ) {
        let pid = window
            .pid
            .ok_or("target window lacks the process identity required by this backend")?;
        Some(ProcessIdentity {
            pid,
            start_time: process_start_time(pid)?,
        })
    } else {
        None
    };
    Ok(WindowIdentity {
        backend,
        id,
        process,
    })
}

fn process_start_time(pid: u32) -> Result<u64, String> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat"))
        .map_err(|error| format!("cannot read target process identity: {error}"))?;
    let fields = stat
        .rsplit_once(") ")
        .ok_or("target process stat is malformed")?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    fields
        .get(19)
        .ok_or("target process stat is incomplete")?
        .parse()
        .map_err(|_| "target process start time is malformed".to_string())
}

fn insert_env(
    attributes: &mut BTreeMap<String, IdentityAttribute>,
    key: &str,
    name: &str,
) -> Result<(), String> {
    attributes.insert(
        key.to_string(),
        IdentityAttribute::Text(required_env(name)?),
    );
    Ok(())
}

fn insert_socket(
    attributes: &mut BTreeMap<String, IdentityAttribute>,
    key: &str,
    path: &Path,
) -> Result<(), String> {
    let metadata =
        fs::metadata(path).map_err(|error| format!("cannot identify {key} {path:?}: {error}"))?;
    attributes.insert(
        format!("{key}_device"),
        IdentityAttribute::Unsigned(metadata.dev()),
    );
    attributes.insert(
        format!("{key}_inode"),
        IdentityAttribute::Unsigned(metadata.ino()),
    );
    Ok(())
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| format!("{name} is unavailable"))
}

fn legacy_hyprland_binding(uid: u32) -> Result<BTreeMap<String, serde_json::Value>, String> {
    let wayland_display = env::var("WAYLAND_DISPLAY")
        .ok()
        .map_or(serde_json::Value::Null, serde_json::Value::String);
    let runtime_dir = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| format!("/run/user/{uid}"));
    Ok(BTreeMap::from([
        (
            "hyprland_instance".to_string(),
            required_env("HYPRLAND_INSTANCE_SIGNATURE")?.into(),
        ),
        ("uid".to_string(), uid.into()),
        ("wayland_display".to_string(), wayland_display),
        ("xdg_runtime_dir".to_string(), runtime_dir.into()),
    ]))
}
