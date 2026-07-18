use crate::diagnostics::hydrate_session_bus_env;
use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
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

const KEY_RELEASED: u32 = 0;
const KEY_PRESSED: u32 = 1;

const AXIS_VERTICAL: u32 = 0;
const AXIS_HORIZONTAL: u32 = 1;

#[derive(Clone)]
pub struct PortalPointerSession {
    connection: Connection,
    session_handle: OwnedObjectPath,
}

#[derive(Clone)]
pub struct PortalKeyboardSession {
    connection: Connection,
    session_handle: OwnedObjectPath,
}

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

pub async fn start_portal_pointer_session() -> Result<PortalPointerSession> {
    hydrate_session_bus_env();

    let connection = Connection::session()
        .await
        .context("failed to connect to session bus for remote desktop portal")?;
    let session_handle = create_remote_desktop_session(&connection).await?;
    select_pointer_devices(&connection, &session_handle).await?;
    let devices = start_remote_desktop_session(&connection, &session_handle).await?;

    if devices & DEVICE_POINTER == 0 {
        bail!("remote desktop portal session started without pointer access");
    }

    Ok(PortalPointerSession {
        connection,
        session_handle,
    })
}

pub async fn start_portal_keyboard_session() -> Result<PortalKeyboardSession> {
    hydrate_session_bus_env();

    let connection = Connection::session()
        .await
        .context("failed to connect to session bus for remote desktop portal")?;
    let session_handle = create_remote_desktop_session(&connection).await?;
    select_keyboard_devices(&connection, &session_handle).await?;
    let devices = start_remote_desktop_session(&connection, &session_handle).await?;

    if devices & DEVICE_KEYBOARD == 0 {
        bail!("remote desktop portal session started without keyboard access");
    }

    Ok(PortalKeyboardSession {
        connection,
        session_handle,
    })
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

async fn select_pointer_devices(connection: &Connection, session: &OwnedObjectPath) -> Result<()> {
    select_devices(connection, session, DEVICE_POINTER, "rd_devices").await
}

async fn select_keyboard_devices(connection: &Connection, session: &OwnedObjectPath) -> Result<()> {
    select_devices(connection, session, DEVICE_KEYBOARD, "rd_keyboard_devices").await
}

async fn select_devices(
    connection: &Connection,
    session: &OwnedObjectPath,
    device_types: u32,
    request_prefix: &str,
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

async fn start_remote_desktop_session(
    connection: &Connection,
    session: &OwnedObjectPath,
) -> Result<u32> {
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
    Ok(devices)
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

#[cfg(test)]
mod tests {
    use super::*;
    use xkeysym::key;

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
}
