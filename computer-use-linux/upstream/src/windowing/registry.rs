use crate::windowing::backends::{cosmic, gnome, hyprland, i3, kwin, niri, x11};
use crate::windowing::types::WindowInfo;
use anyhow::{anyhow, Result};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

pub use cosmic::COSMIC_WAYLAND_BACKEND;
pub use gnome::{GNOME_SHELL_EXTENSION_BACKEND, GNOME_SHELL_INTROSPECT_BACKEND};
pub use hyprland::HYPRLAND_BACKEND;
pub use i3::I3_BACKEND;
pub use kwin::KWIN_BACKEND;
pub use niri::NIRI_BACKEND;
pub use x11::X11_BACKEND;

pub const WINDOW_PERMISSION_HINT: &str = "Computer Use could not access a supported window list backend. Targeted window input requires session-bus access plus GNOME Shell Introspect, the computer-use-linux GNOME Shell extension, the COSMIC Wayland helper, KWin/Plasma DBus scripting, Hyprland hyprctl, Niri IPC, i3-msg, or X11/EWMH tools. On GNOME, run setup_window_targeting to install the extension backend.";

#[derive(Debug, Clone, Copy)]
pub struct BackendDescriptor {
    pub id: &'static str,
    pub failure_label: &'static str,
    pub list_note: &'static str,
    pub missing_hint: &'static str,
    pub can_exact_focus: bool,
    // Keep documentation metadata beside its backend without shipping it in the binary.
    #[cfg(test)]
    support: BackendSupport,
}

#[cfg(test)]
#[derive(Debug, Clone, Copy)]
struct BackendSupport {
    desktop_session: &'static str,
    window_backend: &'static str,
    notes: &'static str,
}

#[derive(Debug, Clone)]
pub struct BackendProbe {
    pub id: &'static str,
    pub ok: bool,
    pub can_list_windows: bool,
    pub can_focus_apps: bool,
    pub can_focus_windows: bool,
    pub detail: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackendKind {
    GnomeExtension,
    GnomeIntrospect,
    Cosmic,
    Kwin,
    Hyprland,
    Niri,
    I3,
    X11,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WindowListPolicy {
    Cached,
    Fresh,
}

const WINDOW_LIST_TTL: Duration = Duration::from_millis(250);

#[derive(Debug, Clone)]
struct CachedWindowList {
    generation: u64,
    captured_at: Instant,
    windows: Vec<WindowInfo>,
}

#[derive(Debug, Default)]
struct WindowCache {
    generation: u64,
    windows: Option<CachedWindowList>,
    preferred_backend: Option<BackendKind>,
}

impl WindowCache {
    fn windows(&self, policy: WindowListPolicy, now: Instant) -> Option<Vec<WindowInfo>> {
        let cached = self.windows.as_ref()?;
        (policy == WindowListPolicy::Cached
            && cached.generation == self.generation
            && now.saturating_duration_since(cached.captured_at) < WINDOW_LIST_TTL)
            .then(|| cached.windows.clone())
    }

    fn invalidate_windows(&mut self) {
        self.generation = self.generation.wrapping_add(1);
        self.windows = None;
    }

    fn record_success(
        &mut self,
        generation: u64,
        backend: BackendKind,
        windows: Vec<WindowInfo>,
        now: Instant,
    ) {
        if generation == self.generation {
            self.preferred_backend = Some(backend);
            self.windows = Some(CachedWindowList {
                generation,
                captured_at: now,
                windows,
            });
        }
    }

    fn record_preferred_failure(&mut self, backend: BackendKind) {
        if self.preferred_backend == Some(backend) {
            self.preferred_backend = None;
            self.invalidate_windows();
        }
    }
}

fn window_cache() -> MutexGuard<'static, WindowCache> {
    static CACHE: OnceLock<Mutex<WindowCache>> = OnceLock::new();
    CACHE
        .get_or_init(|| Mutex::new(WindowCache::default()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn invalidate_window_cache() {
    window_cache().invalidate_windows();
}

const BACKEND_ORDER: &[BackendKind] = &[
    BackendKind::GnomeExtension,
    BackendKind::GnomeIntrospect,
    BackendKind::Cosmic,
    BackendKind::Kwin,
    BackendKind::Hyprland,
    BackendKind::Niri,
    BackendKind::I3,
    // Generic X11/EWMH: last, so a session-native backend always wins first.
    BackendKind::X11,
];

const DESCRIPTORS: &[BackendDescriptor] = &[
    BackendDescriptor {
        id: GNOME_SHELL_EXTENSION_BACKEND,
        failure_label: "computer-use-linux GNOME Shell extension",
        list_note: "Window list came from the computer-use-linux GNOME Shell extension. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On GNOME, run setup_window_targeting to install the optional GNOME Shell extension backend.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "GNOME Wayland",
            window_backend: "GNOME Shell extension first, `org.gnome.Shell.Introspect` fallback",
            notes: "Full target. The extension provides exact window activation when GNOME blocks native introspection; Introspect can list windows and focus apps by `app_id` when allowed.",
        },
    },
    BackendDescriptor {
        id: GNOME_SHELL_INTROSPECT_BACKEND,
        failure_label: "GNOME Shell Introspect",
        list_note: "Window list came from GNOME Shell Introspect. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On GNOME, ensure org.gnome.Shell.Introspect is available on the session bus.",
        can_exact_focus: false,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "GNOME X11",
            window_backend: "`org.gnome.Shell.Introspect` when allowed",
            notes: "AT-SPI and `ydotool` work; exact per-window focus may be unavailable without the extension backend.",
        },
    },
    BackendDescriptor {
        id: COSMIC_WAYLAND_BACKEND,
        failure_label: "COSMIC helper",
        list_note: "Window list came from the COSMIC Wayland helper. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On COSMIC, ensure the bundled COSMIC helper is present and can connect to the session.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "COSMIC Wayland",
            window_backend: "`computer-use-linux-cosmic` helper",
            notes: "Installed automatically by `./install.sh`, `cargo install`, and npm. For custom/manual layouts, put the helper next to the main binary, on `PATH`, or set `COMPUTER_USE_LINUX_COSMIC_HELPER`.",
        },
    },
    BackendDescriptor {
        id: KWIN_BACKEND,
        failure_label: "KWin",
        list_note: "Window list came from KWin/Plasma DBus scripting. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On KDE/Plasma, ensure KWin exposes org.kde.KWin scripting on the session bus.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "KDE Plasma / KWin",
            window_backend: "temporary KWin DBus scripting",
            notes: "Lists and focuses windows through `org.kde.KWin` scripting when the session bus exposes it.",
        },
    },
    BackendDescriptor {
        id: HYPRLAND_BACKEND,
        failure_label: "Hyprland",
        list_note: "Window list came from Hyprland hyprctl. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On Hyprland, ensure hyprctl is available in the session.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "Hyprland",
            window_backend: "`hyprctl clients -j` and `hyprctl dispatch focuswindow`",
            notes: "Requires `hyprctl` in the desktop session.",
        },
    },
    BackendDescriptor {
        id: NIRI_BACKEND,
        failure_label: "Niri",
        list_note: "Window list came from Niri IPC. Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On Niri, ensure NIRI_SOCKET is available and niri msg can reach the active compositor.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "Niri",
            window_backend: "`niri msg --json windows` and `niri msg action focus-window`",
            notes: "Requires `NIRI_SOCKET` and the `niri` command from the active compositor session.",
        },
    },
    BackendDescriptor {
        id: I3_BACKEND,
        failure_label: "i3",
        list_note: "Window list came from i3-msg. Terminal windows may include best-effort PTY and active-process context when xprop and the process tree are readable.",
        missing_hint: "On i3, ensure i3-msg can reach the active i3 IPC socket.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "i3",
            window_backend: "`i3-msg`; optional `xprop` for PID hydration",
            notes: "Lists and focuses i3 windows over the active i3 IPC socket.",
        },
    },
    BackendDescriptor {
        id: X11_BACKEND,
        failure_label: "X11/EWMH",
        list_note: "Window list came from X11/EWMH (wmctrl). Terminal windows may include best-effort PTY and active-process context when the process tree is readable.",
        missing_hint: "On other X11 window managers (Cinnamon, MATE, Xfce, Openbox…), ensure wmctrl and xprop are installed.",
        can_exact_focus: true,
        #[cfg(test)]
        support: BackendSupport {
            desktop_session: "Generic X11 / Xfce / other EWMH WMs",
            window_backend: "native X11/EWMH connection; `wmctrl`/`xprop` fallback",
            notes: "Lists and focuses through one persistent X11 connection, with event-invalidated snapshots. `wmctrl` remains the move/resize and compatibility fallback.",
        },
    },
];

pub fn descriptors() -> &'static [BackendDescriptor] {
    DESCRIPTORS
}

pub fn descriptor(id: &str) -> Option<&'static BackendDescriptor> {
    DESCRIPTORS.iter().find(|descriptor| descriptor.id == id)
}

pub fn list_note(id: &str) -> &'static str {
    descriptor(id)
        .map(|descriptor| descriptor.list_note)
        .unwrap_or_else(|| {
            descriptor(GNOME_SHELL_INTROSPECT_BACKEND)
                .unwrap()
                .list_note
        })
}

pub fn backend_can_exact_focus(id: &str) -> bool {
    descriptor(id).is_some_and(|descriptor| descriptor.can_exact_focus)
}

pub async fn list_windows() -> Result<Vec<WindowInfo>> {
    list_windows_with_policy(WindowListPolicy::Cached).await
}

pub(crate) async fn list_windows_with_policy(policy: WindowListPolicy) -> Result<Vec<WindowInfo>> {
    let now = Instant::now();
    let (mut generation, preferred_backend) = {
        let cache = window_cache();
        if let Some(windows) = cache.windows(policy, now) {
            return Ok(windows);
        }
        (cache.generation, cache.preferred_backend)
    };

    let mut errors = Vec::new();
    if let Some(backend) = preferred_backend {
        if let Some(windows) =
            usable_backend_windows(backend, list_windows_for(backend).await, &mut errors)
        {
            window_cache().record_success(generation, backend, windows.clone(), Instant::now());
            return Ok(windows);
        }
        let mut cache = window_cache();
        cache.record_preferred_failure(backend);
        generation = cache.generation;
    }

    for backend in BACKEND_ORDER {
        if Some(*backend) == preferred_backend {
            continue;
        }
        if let Some(windows) =
            usable_backend_windows(*backend, list_windows_for(*backend).await, &mut errors)
        {
            window_cache().record_success(generation, *backend, windows.clone(), Instant::now());
            return Ok(windows);
        }
    }
    Err(anyhow!(errors.join("; ")))
}

fn usable_backend_windows(
    backend: BackendKind,
    result: Result<Vec<WindowInfo>>,
    errors: &mut Vec<String>,
) -> Option<Vec<WindowInfo>> {
    match result {
        Ok(windows) if !windows.is_empty() => Some(windows),
        Ok(_) => {
            errors.push(format!("{} returned no windows", backend.failure_label()));
            None
        }
        Err(error) => {
            errors.push(format!("{} failed: {error:#}", backend.failure_label()));
            None
        }
    }
}

async fn list_windows_for(backend: BackendKind) -> Result<Vec<WindowInfo>> {
    match backend {
        BackendKind::GnomeExtension => gnome::list_extension_windows().await,
        BackendKind::GnomeIntrospect => gnome::list_introspect_windows().await,
        BackendKind::Cosmic => cosmic::list_windows(),
        BackendKind::Kwin => kwin::list_windows().await,
        BackendKind::Hyprland => hyprland::list_windows(),
        BackendKind::Niri => niri::list_windows(),
        BackendKind::I3 => i3::list_windows(),
        BackendKind::X11 => x11::list_windows(),
    }
}

pub async fn activate_window(window: &WindowInfo) -> Result<()> {
    let result = match window.backend.as_str() {
        GNOME_SHELL_EXTENSION_BACKEND => gnome::activate_extension_window(window.window_id).await,
        GNOME_SHELL_INTROSPECT_BACKEND => {
            let app_id = window
                .app_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty());
            match app_id {
                Some(app_id) => gnome::focus_app(app_id).await,
                None => Err(anyhow!(
                    "GNOME Shell can only focus by app_id; the matched window has no app_id"
                )),
            }
        }
        COSMIC_WAYLAND_BACKEND => cosmic::activate_window(window.window_id),
        KWIN_BACKEND => kwin::activate_window(window.window_id).await,
        HYPRLAND_BACKEND => hyprland::activate_window(window.window_id),
        NIRI_BACKEND => niri::activate_window(window.window_id),
        I3_BACKEND => i3::activate_window(window.window_id),
        X11_BACKEND => x11::activate_window(window.window_id),
        backend => Err(anyhow!(
            "Unsupported window backend for activation: {backend}"
        )),
    };
    invalidate_window_cache();
    result
}

pub async fn move_window(window: &WindowInfo, x: i32, y: i32) -> Result<String> {
    let result = match window.backend.as_str() {
        GNOME_SHELL_EXTENSION_BACKEND => {
            gnome::move_extension_window(window.window_id, x, y).await
        }
        X11_BACKEND => x11::move_window(window.window_id, x, y),
        backend => Err(anyhow!(
            "Window backend {backend} cannot move windows; move_window needs the computer-use-linux GNOME Shell extension or a generic X11/EWMH session."
        )),
    };
    invalidate_window_cache();
    result
}

pub async fn resize_window(window: &WindowInfo, width: i32, height: i32) -> Result<String> {
    let result = match window.backend.as_str() {
        GNOME_SHELL_EXTENSION_BACKEND => {
            gnome::resize_extension_window(window.window_id, width, height).await
        }
        X11_BACKEND => x11::resize_window(window.window_id, width, height),
        backend => Err(anyhow!(
            "Window backend {backend} cannot resize windows; resize_window needs the computer-use-linux GNOME Shell extension or a generic X11/EWMH session."
        )),
    };
    invalidate_window_cache();
    result
}

pub fn focused_window_override() -> Option<WindowInfo> {
    cosmic::focused_window()
        .ok()
        .flatten()
        .or_else(x11::focused_window)
}

pub fn probe_backends() -> Vec<BackendProbe> {
    vec![
        gnome::probe_extension(),
        gnome::probe_introspect(),
        cosmic::probe(),
        kwin::probe(),
        hyprland::probe(),
        niri::probe(),
        i3::probe(),
        x11::probe(),
    ]
}

impl BackendKind {
    fn id(self) -> &'static str {
        match self {
            BackendKind::GnomeExtension => GNOME_SHELL_EXTENSION_BACKEND,
            BackendKind::GnomeIntrospect => GNOME_SHELL_INTROSPECT_BACKEND,
            BackendKind::Cosmic => COSMIC_WAYLAND_BACKEND,
            BackendKind::Kwin => KWIN_BACKEND,
            BackendKind::Hyprland => HYPRLAND_BACKEND,
            BackendKind::Niri => NIRI_BACKEND,
            BackendKind::I3 => I3_BACKEND,
            BackendKind::X11 => X11_BACKEND,
        }
    }

    fn failure_label(self) -> &'static str {
        descriptor(self.id())
            .map(|item| item.failure_label)
            .unwrap_or(self.id())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::windowing::types::WindowBounds;
    use std::fs;

    fn window(backend: &str) -> WindowInfo {
        WindowInfo {
            window_id: 1,
            title: Some("Codex".to_string()),
            app_id: Some("codex-desktop".to_string()),
            wm_class: Some("codex-desktop".to_string()),
            pid: Some(1234),
            bounds: Some(WindowBounds {
                x: Some(0),
                y: Some(0),
                width: 800,
                height: 600,
            }),
            workspace: None,
            focused: true,
            hidden: false,
            client_type: Some("wayland".to_string()),
            backend: backend.to_string(),
            terminal: None,
        }
    }

    #[test]
    fn skips_empty_backend_results_so_later_backends_can_answer() {
        let mut errors = Vec::new();

        assert!(
            usable_backend_windows(BackendKind::GnomeIntrospect, Ok(Vec::new()), &mut errors,)
                .is_none()
        );

        let windows = usable_backend_windows(
            BackendKind::Kwin,
            Ok(vec![window(KWIN_BACKEND)]),
            &mut errors,
        )
        .expect("non-empty backend result should be accepted");

        assert_eq!(windows[0].backend, KWIN_BACKEND);
        assert_eq!(errors, vec!["GNOME Shell Introspect returned no windows"]);
    }

    #[test]
    fn records_backend_failures_with_registry_labels() {
        let mut errors = Vec::new();

        assert!(usable_backend_windows(
            BackendKind::Kwin,
            Err(anyhow!("loadScript failed")),
            &mut errors,
        )
        .is_none());

        assert_eq!(errors, vec!["KWin failed: loadScript failed"]);
    }

    #[test]
    fn readme_support_matrix_matches_backend_registry() {
        const START: &str = "<!-- BEGIN GENERATED BACKEND SUPPORT MATRIX -->";
        const END: &str = "<!-- END GENERATED BACKEND SUPPORT MATRIX -->";

        let readme = fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/README.md"))
            .expect("README should be readable");
        let documented = readme
            .split_once(START)
            .and_then(|(_, remainder)| remainder.split_once(END))
            .map(|(matrix, _)| matrix.trim())
            .expect("README should contain generated support matrix markers");
        let mut expected =
            String::from("| Desktop/session | Window backend | Notes |\n| --- | --- | --- |");
        for descriptor in descriptors() {
            let support = descriptor.support;
            expected.push_str(&format!(
                "\n| {} | {} | {} |",
                support.desktop_session, support.window_backend, support.notes
            ));
        }

        assert_eq!(
            documented, expected,
            "README support matrix drifted from the backend registry"
        );
    }

    #[test]
    fn cached_window_lists_honor_policy_ttl_and_generation() {
        let now = Instant::now();
        let mut cache = WindowCache::default();
        cache.record_success(0, BackendKind::Kwin, vec![window(KWIN_BACKEND)], now);

        let cached = cache.windows(WindowListPolicy::Cached, now).unwrap();
        assert_eq!(cached[0].backend, KWIN_BACKEND);
        assert!(cache.windows(WindowListPolicy::Fresh, now).is_none());
        assert!(cache
            .windows(WindowListPolicy::Cached, now + WINDOW_LIST_TTL)
            .is_none());

        cache.invalidate_windows();
        assert!(cache.windows(WindowListPolicy::Cached, now).is_none());
        assert_eq!(cache.preferred_backend, Some(BackendKind::Kwin));
        cache.record_success(
            0,
            BackendKind::Hyprland,
            vec![window(HYPRLAND_BACKEND)],
            now,
        );
        assert_eq!(cache.preferred_backend, Some(BackendKind::Kwin));
        cache.record_preferred_failure(BackendKind::Kwin);
        assert!(cache.windows(WindowListPolicy::Cached, now).is_none());
    }
}
