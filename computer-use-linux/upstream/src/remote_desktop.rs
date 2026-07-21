use crate::diagnostics::hydrate_session_bus_env;
use anyhow::{bail, Context, Result};
use fs2::FileExt;
use futures_util::StreamExt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::{collections::HashMap, time::Duration};
use xkeysym::Keysym;
use zbus::{
    proxy::SignalStream,
    zvariant::{OwnedObjectPath, OwnedValue, Value},
    Connection, Proxy,
};

const PORTAL_DESKTOP_SERVICE: &str = "org.freedesktop.portal.Desktop";
const PORTAL_DESKTOP_PATH: &str = "/org/freedesktop/portal/desktop";
const PORTAL_REMOTE_DESKTOP_INTERFACE: &str = "org.freedesktop.portal.RemoteDesktop";
const PORTAL_REQUEST_INTERFACE: &str = "org.freedesktop.portal.Request";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

const DEVICE_KEYBOARD: u32 = 1;
const DEVICE_POINTER: u32 = 2;
const PERSIST_MODE_EXPLICITLY_REVOKED: u32 = 2;
const MAX_RESTORE_TOKEN_BYTES: usize = 4096;

const KEY_RELEASED: u32 = 0;
const KEY_PRESSED: u32 = 1;

const AXIS_VERTICAL: u32 = 0;
const AXIS_HORIZONTAL: u32 = 1;

#[derive(Clone)]
pub struct PortalSession {
    connection: Connection,
    session_handle: OwnedObjectPath,
    devices: u32,
}

impl PortalSession {
    pub(crate) fn has_pointer(&self) -> bool {
        self.devices & DEVICE_POINTER != 0
    }

    pub(crate) fn has_keyboard(&self) -> bool {
        self.devices & DEVICE_KEYBOARD != 0
    }
}

pub type PortalPointerSession = PortalSession;
pub type PortalKeyboardSession = PortalSession;

#[derive(Debug)]
pub(crate) enum PortalActionError {
    PreDispatch(anyhow::Error),
    MayHaveDelivered(anyhow::Error),
}

impl PortalActionError {
    pub(crate) fn can_fallback_to_ydotool(&self) -> bool {
        matches!(self, Self::PreDispatch(_))
    }
}

impl std::fmt::Display for PortalActionError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::PreDispatch(error) | Self::MayHaveDelivered(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for PortalActionError {}
#[derive(Debug, Clone, Copy)]
pub enum ScrollDirection {
    Up,
    Down,
    Left,
    Right,
}

enum PortalPersistence<'a> {
    Disabled,
    Enabled { restore_token: Option<&'a str> },
}

pub async fn start_portal_session() -> Result<PortalSession> {
    hydrate_session_bus_env();

    let restore_store = if let Some(restore_path) = restore_token_path() {
        let lock_path = restore_path.with_extension("lock");
        match tokio::task::spawn_blocking(move || restore_token_lock(&lock_path)).await {
            Ok(Ok(guard)) => Some((restore_path, guard)),
            Ok(Err(_)) | Err(_) => None,
        }
    } else {
        None
    };
    let connection = Connection::session()
        .await
        .context("failed to connect to session bus for remote desktop portal")?;
    let session_handle = create_remote_desktop_session(&connection).await?;
    let restore_token = restore_store
        .as_ref()
        .and_then(|(path, _guard)| read_restore_token(path).ok().flatten());
    let persistence = match restore_store.as_ref() {
        Some(_) => PortalPersistence::Enabled {
            restore_token: restore_token.as_deref(),
        },
        None => PortalPersistence::Disabled,
    };
    select_devices(
        &connection,
        &session_handle,
        DEVICE_POINTER | DEVICE_KEYBOARD,
        "rd_devices",
        persistence,
    )
    .await?;
    let started = start_remote_desktop_session(&connection, &session_handle).await?;

    if started.devices & (DEVICE_POINTER | DEVICE_KEYBOARD) == 0 {
        bail!("remote desktop portal session started without pointer or keyboard access");
    }
    if let Some((restore_path, _guard)) = restore_store {
        let _ = update_restore_token(
            &restore_path,
            restore_token.is_some(),
            started.restore_token.as_deref(),
        );
    }

    Ok(PortalSession {
        connection,
        session_handle,
        devices: started.devices,
    })
}

#[allow(dead_code)]
pub async fn start_portal_pointer_session() -> Result<PortalPointerSession> {
    let session = start_portal_session().await?;
    if !session.has_pointer() {
        bail!("remote desktop portal session started without pointer access");
    }
    Ok(session)
}

#[allow(dead_code)]
pub async fn start_portal_keyboard_session() -> Result<PortalKeyboardSession> {
    let session = start_portal_session().await?;
    if !session.has_keyboard() {
        bail!("remote desktop portal session started without keyboard access");
    }
    Ok(session)
}

pub fn keysyms_for_text(text: &str) -> Result<Vec<i32>> {
    text.chars()
        .map(|ch| {
            let keysym = Keysym::from_char(ch);
            if keysym == Keysym::NoSymbol {
                bail!(
                    "character U+{:04X} cannot be represented as an X11 keysym",
                    ch as u32
                );
            }
            i32::try_from(keysym.raw()).context("X11 keysym did not fit in D-Bus int32")
        })
        .collect()
}

pub async fn scroll(
    session: &PortalPointerSession,
    direction: ScrollDirection,
    steps: i32,
) -> std::result::Result<(), PortalActionError> {
    let proxy = remote_desktop_proxy(&session.connection)
        .await
        .map_err(PortalActionError::PreDispatch)?;

    let (axis, steps) = match direction {
        ScrollDirection::Up => (AXIS_VERTICAL, steps.max(1)),
        ScrollDirection::Down => (AXIS_VERTICAL, -steps.max(1)),
        ScrollDirection::Left => (AXIS_HORIZONTAL, steps.max(1)),
        ScrollDirection::Right => (AXIS_HORIZONTAL, -steps.max(1)),
    };

    notify_pointer_axis_discrete(&proxy, &session.session_handle, axis, steps)
        .await
        .map_err(PortalActionError::MayHaveDelivered)
}

pub async fn type_text_with_keysyms(
    session: &PortalKeyboardSession,
    keysyms: &[i32],
) -> Result<()> {
    let proxy = remote_desktop_proxy(&session.connection).await?;
    for keysym in keysyms {
        notify_keyboard_keysym(&proxy, &session.session_handle, *keysym, KEY_PRESSED).await?;
        tokio::time::sleep(Duration::from_millis(5)).await;
        notify_keyboard_keysym(&proxy, &session.session_handle, *keysym, KEY_RELEASED).await?;
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    Ok(())
}

pub async fn press_keycode_chord(
    session: &PortalKeyboardSession,
    modifiers: &[i32],
    keycode: i32,
) -> Result<()> {
    let proxy = remote_desktop_proxy(&session.connection).await?;
    for modifier in modifiers {
        notify_keyboard_keycode(&proxy, &session.session_handle, *modifier, KEY_PRESSED).await?;
    }
    notify_keyboard_keycode(&proxy, &session.session_handle, keycode, KEY_PRESSED).await?;
    tokio::time::sleep(Duration::from_millis(35)).await;
    notify_keyboard_keycode(&proxy, &session.session_handle, keycode, KEY_RELEASED).await?;
    for modifier in modifiers.iter().rev() {
        notify_keyboard_keycode(&proxy, &session.session_handle, *modifier, KEY_RELEASED).await?;
    }
    Ok(())
}

async fn create_remote_desktop_session(connection: &Connection) -> Result<OwnedObjectPath> {
    let remote_proxy = remote_desktop_proxy(connection).await?;
    let (request_path, mut response_stream) =
        portal_request_stream(connection, "rd_create").await?;
    let session_token = request_token("rd_session");
    let mut options: HashMap<&str, Value<'_>> = HashMap::new();
    options.insert(
        "handle_token",
        Value::from(last_path_component(&request_path)),
    );
    options.insert("session_handle_token", Value::from(session_token.as_str()));

    let handle: OwnedObjectPath = remote_proxy
        .call("CreateSession", &(options))
        .await
        .context("RemoteDesktop CreateSession call failed")?;
    let (response_code, results) =
        await_portal_response(connection, handle, &request_path, &mut response_stream).await?;
    if response_code != 0 {
        bail!("RemoteDesktop CreateSession denied or cancelled with response code {response_code}");
    }

    let session_handle: String = results
        .get("session_handle")
        .context("RemoteDesktop CreateSession response did not include session_handle")?
        .try_clone()
        .context("failed to clone session_handle")?
        .try_into()
        .context("RemoteDesktop session_handle was not a string")?;
    OwnedObjectPath::try_from(session_handle)
        .context("RemoteDesktop session_handle was not a valid object path")
}

async fn select_devices(
    connection: &Connection,
    session: &OwnedObjectPath,
    device_types: u32,
    request_prefix: &str,
    persistence: PortalPersistence<'_>,
) -> Result<()> {
    let remote_proxy = remote_desktop_proxy(connection).await?;
    let (request_path, mut response_stream) =
        portal_request_stream(connection, request_prefix).await?;
    let mut options: HashMap<&str, Value<'_>> = HashMap::new();
    options.insert(
        "handle_token",
        Value::from(last_path_component(&request_path)),
    );
    options.insert("types", Value::from(device_types));
    let portal_version = remote_proxy
        .get_property::<u32>("version")
        .await
        .unwrap_or(1);
    match (portal_version >= 2, persistence) {
        (true, PortalPersistence::Enabled { restore_token }) => {
            options.insert("persist_mode", Value::from(PERSIST_MODE_EXPLICITLY_REVOKED));
            if let Some(token) = restore_token {
                options.insert("restore_token", Value::from(token));
            }
        }
        (true, PortalPersistence::Disabled)
        | (false, PortalPersistence::Disabled)
        | (false, PortalPersistence::Enabled { .. }) => {}
    }

    let handle: OwnedObjectPath = remote_proxy
        .call("SelectDevices", &(session, options))
        .await
        .context("RemoteDesktop SelectDevices call failed")?;
    let (response_code, _) =
        await_portal_response(connection, handle, &request_path, &mut response_stream).await?;
    if response_code != 0 {
        bail!("RemoteDesktop SelectDevices denied or cancelled with response code {response_code}");
    }
    Ok(())
}

struct StartedSession {
    devices: u32,
    restore_token: Option<String>,
}

async fn start_remote_desktop_session(
    connection: &Connection,
    session: &OwnedObjectPath,
) -> Result<StartedSession> {
    let remote_proxy = remote_desktop_proxy(connection).await?;
    let (request_path, mut response_stream) = portal_request_stream(connection, "rd_start").await?;
    let mut options: HashMap<&str, Value<'_>> = HashMap::new();
    options.insert(
        "handle_token",
        Value::from(last_path_component(&request_path)),
    );

    let handle: OwnedObjectPath = remote_proxy
        .call("Start", &(session, "", options))
        .await
        .context("RemoteDesktop Start call failed")?;
    let (response_code, results) =
        await_portal_response(connection, handle, &request_path, &mut response_stream).await?;
    if response_code != 0 {
        bail!("RemoteDesktop Start denied or cancelled with response code {response_code}");
    }

    let devices = results
        .get("devices")
        .and_then(|value| u32::try_from(value).ok())
        .unwrap_or_default();
    let restore_token = results
        .get("restore_token")
        .and_then(|value| value.try_clone().ok())
        .and_then(|value| String::try_from(value).ok())
        .filter(|token| valid_restore_token(token));
    Ok(StartedSession {
        devices,
        restore_token,
    })
}

async fn notify_pointer_axis_discrete(
    proxy: &Proxy<'_>,
    session: &OwnedObjectPath,
    axis: u32,
    steps: i32,
) -> Result<()> {
    let options: HashMap<&str, Value<'_>> = HashMap::new();
    let _: () = proxy
        .call(
            "NotifyPointerAxisDiscrete",
            &(session, options, axis, steps),
        )
        .await
        .context("RemoteDesktop NotifyPointerAxisDiscrete failed")?;
    Ok(())
}

async fn notify_keyboard_keysym(
    proxy: &Proxy<'_>,
    session: &OwnedObjectPath,
    keysym: i32,
    state: u32,
) -> Result<()> {
    let options: HashMap<&str, Value<'_>> = HashMap::new();
    let _: () = proxy
        .call("NotifyKeyboardKeysym", &(session, options, keysym, state))
        .await
        .context("RemoteDesktop NotifyKeyboardKeysym failed")?;
    Ok(())
}

async fn notify_keyboard_keycode(
    proxy: &Proxy<'_>,
    session: &OwnedObjectPath,
    keycode: i32,
    state: u32,
) -> Result<()> {
    let options: HashMap<&str, Value<'_>> = HashMap::new();
    let _: () = proxy
        .call("NotifyKeyboardKeycode", &(session, options, keycode, state))
        .await
        .context("RemoteDesktop NotifyKeyboardKeycode failed")?;
    Ok(())
}

async fn remote_desktop_proxy(connection: &Connection) -> Result<Proxy<'_>> {
    Proxy::new(
        connection,
        PORTAL_DESKTOP_SERVICE,
        PORTAL_DESKTOP_PATH,
        PORTAL_REMOTE_DESKTOP_INTERFACE,
    )
    .await
    .context("failed to create RemoteDesktop portal proxy")
}

async fn portal_request_stream<'a>(
    connection: &'a Connection,
    prefix: &str,
) -> Result<(String, SignalStream<'a>)> {
    let unique_name = connection
        .unique_name()
        .context("session bus connection has no unique name")?;
    let token = request_token(prefix);
    let request_path = request_path(unique_name.as_str(), &token);
    let request_proxy = Proxy::new(
        connection,
        PORTAL_DESKTOP_SERVICE,
        request_path.as_str(),
        PORTAL_REQUEST_INTERFACE,
    )
    .await
    .context("failed to create portal request proxy")?;
    let response_stream = request_proxy
        .receive_signal("Response")
        .await
        .context("failed to subscribe to portal request response")?;
    Ok((request_path, response_stream))
}

async fn await_portal_response(
    connection: &Connection,
    handle: OwnedObjectPath,
    expected_request_path: &str,
    response_stream: &mut SignalStream<'_>,
) -> Result<(u32, HashMap<String, OwnedValue>)> {
    if handle.as_str() != expected_request_path {
        *response_stream = Proxy::new(
            connection,
            PORTAL_DESKTOP_SERVICE,
            handle.as_str(),
            PORTAL_REQUEST_INTERFACE,
        )
        .await
        .context("failed to create returned portal request proxy")?
        .receive_signal("Response")
        .await
        .context("failed to subscribe to returned portal response")?;
    }

    let response = tokio::time::timeout(REQUEST_TIMEOUT, response_stream.next())
        .await
        .context("timed out waiting for portal response")?
        .context("portal response stream ended")?;
    response
        .body()
        .deserialize()
        .context("failed to decode portal response")
}

fn request_path(unique_name: &str, token: &str) -> String {
    format!(
        "/org/freedesktop/portal/desktop/request/{}/{}",
        unique_name.trim_start_matches(':').replace('.', "_"),
        token
    )
}

fn last_path_component(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

fn request_token(prefix: &str) -> String {
    format!(
        "{prefix}_{}_{:?}",
        std::process::id(),
        std::time::SystemTime::now()
    )
    .chars()
    .map(|ch| match ch {
        'a'..='z' | 'A'..='Z' | '0'..='9' | '_' => ch,
        _ => '_',
    })
    .collect()
}

#[derive(serde::Deserialize, serde::Serialize)]
struct RestoreTokenFile {
    version: u8,
    token: String,
}

fn restore_token_path() -> Option<PathBuf> {
    let root = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .filter(|path| path.is_absolute())
                .map(|home| home.join(".local/state"))
        })?;
    Some(root.join("computer-use-linux/remote-desktop-restore-token.json"))
}

fn valid_restore_token(token: &str) -> bool {
    !token.is_empty() && token.len() <= MAX_RESTORE_TOKEN_BYTES && !token.contains('\0')
}

fn prepare_private_parent(path: &Path) -> Result<()> {
    let parent = path
        .parent()
        .context("restore-token path had no parent directory")?;
    fs::create_dir_all(parent).with_context(|| format!("failed to create {}", parent.display()))?;
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
        .with_context(|| format!("failed to secure {}", parent.display()))
}

fn restore_token_lock(path: &Path) -> Result<fs::File> {
    prepare_private_parent(path)?;
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("failed to open {}", path.display()))?;
    file.lock_exclusive()
        .with_context(|| format!("failed to lock {}", path.display()))?;
    Ok(file)
}

fn read_restore_token(path: &Path) -> Result<Option<String>> {
    let serialized = match fs::read(path) {
        Ok(serialized) => serialized,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error).with_context(|| format!("failed to read {}", path.display()));
        }
    };
    if serialized.len() > MAX_RESTORE_TOKEN_BYTES + 128 {
        return Ok(None);
    }
    let record: RestoreTokenFile = match serde_json::from_slice(&serialized) {
        Ok(record) => record,
        Err(_) => return Ok(None),
    };
    Ok((record.version == 1 && valid_restore_token(&record.token)).then_some(record.token))
}

fn update_restore_token(path: &Path, consumed_old: bool, replacement: Option<&str>) -> Result<()> {
    let replacement = replacement.filter(|token| valid_restore_token(token));
    let Some(token) = replacement else {
        if consumed_old {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(error)
                        .with_context(|| format!("failed to remove {}", path.display()));
                }
            }
        }
        return Ok(());
    };

    prepare_private_parent(path)?;
    let serialized = serde_json::to_vec(&RestoreTokenFile {
        version: 1,
        token: token.to_string(),
    })?;
    let suffix = request_token("restore_token");
    let temporary = path.with_extension(format!("tmp.{suffix}"));
    let write_result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)
            .with_context(|| format!("failed to create {}", temporary.display()))?;
        file.write_all(&serialized)
            .with_context(|| format!("failed to write {}", temporary.display()))?;
        file.sync_all()
            .with_context(|| format!("failed to sync {}", temporary.display()))?;
        fs::rename(&temporary, path).with_context(|| {
            format!(
                "failed to replace restore token {} with {}",
                path.display(),
                temporary.display()
            )
        })?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    write_result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use xkeysym::key;

    fn restore_token_test_path(label: &str) -> PathBuf {
        std::env::temp_dir()
            .join(format!(
                "computer-use-linux-{label}-{}-{}",
                std::process::id(),
                request_token("test")
            ))
            .join("restore-token.json")
    }

    #[test]
    fn keysyms_for_url_text_round_trips_to_literal_characters() {
        let text = "https://example.com:8080/page#anchor";
        let keysyms = keysyms_for_text(text).expect("URL should map to keysyms");

        let round_tripped = keysyms
            .iter()
            .map(|keysym| {
                Keysym::new(*keysym as u32)
                    .key_char()
                    .expect("keysym should map back to a character")
            })
            .collect::<String>();

        assert_eq!(round_tripped, text);
    }

    #[test]
    fn keysyms_for_layout_sensitive_ascii_use_literal_symbols() {
        assert_eq!(
            keysyms_for_text(":#/?@").expect("symbols should map to keysyms"),
            vec![
                key::colon as i32,
                key::numbersign as i32,
                key::slash as i32,
                key::question as i32,
                key::at as i32,
            ]
        );
    }

    #[test]
    fn keysyms_for_non_ascii_use_legacy_and_unicode_mapped_values() {
        assert_eq!(
            keysyms_for_text("ä€😉").expect("non-ASCII text should map to keysyms"),
            vec![key::adiaeresis as i32, key::EuroSign as i32, 0x0101_F609]
        );
    }

    #[test]
    fn keysyms_for_text_rejects_unicode_non_symbols_before_input() {
        let error = keysyms_for_text("\u{FDD0}")
            .expect_err("Unicode non-characters should not be emitted through the portal")
            .to_string();

        assert!(error.contains("U+FDD0"));
    }

    #[test]
    fn restore_tokens_rotate_atomically_and_are_private() {
        let path = restore_token_test_path("portal-restore-token");
        update_restore_token(&path, false, Some("first-token")).expect("write first token");
        assert_eq!(
            read_restore_token(&path).expect("read first token"),
            Some("first-token".to_string())
        );
        assert_eq!(
            fs::metadata(path.parent().expect("parent"))
                .expect("parent metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&path)
                .expect("token metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );

        update_restore_token(&path, true, Some("second-token")).expect("rotate token");
        assert_eq!(
            read_restore_token(&path).expect("read second token"),
            Some("second-token".to_string())
        );
        update_restore_token(&path, true, None).expect("remove consumed token");
        assert_eq!(read_restore_token(&path).expect("read removed token"), None);
        let _ = fs::remove_dir(path.parent().expect("parent"));
    }

    #[test]
    fn malformed_or_unbounded_restore_tokens_are_ignored() {
        let path = restore_token_test_path("portal-invalid-token");
        prepare_private_parent(&path).expect("prepare parent");
        fs::write(&path, b"not-json").expect("write invalid token");
        assert_eq!(read_restore_token(&path).expect("read invalid token"), None);
        assert!(!valid_restore_token(""));
        assert!(!valid_restore_token(
            &"x".repeat(MAX_RESTORE_TOKEN_BYTES + 1)
        ));
        assert!(!valid_restore_token("bad\0token"));
        let _ = fs::remove_file(&path);
        let _ = fs::remove_dir(path.parent().expect("parent"));
    }
}
