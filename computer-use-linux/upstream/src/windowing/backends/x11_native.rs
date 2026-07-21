//! Persistent native X11/EWMH transport for the generic X11 backend.
//!
//! The connection stays open for the MCP process lifetime. Root and client
//! events invalidate the bounded snapshot cache; callers still get a fresh
//! `_NET_ACTIVE_WINDOW` read for focused-window verification.

use crate::windowing::backends::x11::X11_BACKEND;
use crate::windowing::types::{WindowBounds, WindowInfo};
use anyhow::{bail, Context, Result};
use std::env;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};
use x11rb::connection::Connection;
use x11rb::protocol::xproto::{
    Atom, AtomEnum, ChangeWindowAttributesAux, ClientMessageData, ClientMessageEvent,
    ConnectionExt, EventMask, Window,
};
use x11rb::protocol::Event;
use x11rb::rust_connection::RustConnection;
use x11rb::CURRENT_TIME;

const SNAPSHOT_TTL: Duration = Duration::from_millis(250);
// X11 expresses get_property's maximum reply size in four-byte units. Keep
// malformed or unexpectedly large root properties from growing without bound.
const MAX_PROPERTY_LONGS: u32 = 16 * 1024;

struct Atoms {
    active_window: Atom,
    client_list: Atom,
    client_list_stacking: Atom,
    net_supported: Atom,
    net_wm_desktop: Atom,
    net_wm_name: Atom,
    net_wm_pid: Atom,
    net_wm_state: Atom,
    net_wm_state_hidden: Atom,
    utf8_string: Atom,
}

#[derive(Clone)]
struct CachedSnapshot {
    captured_at: Instant,
    windows: Vec<WindowInfo>,
}

struct NativeSession {
    connection: RustConnection,
    display: String,
    root: Window,
    atoms: Atoms,
    dirty: bool,
    snapshot: Option<CachedSnapshot>,
}

pub(super) fn probe() -> Result<String> {
    with_session(|session| {
        session.require_ewmh_support()?;
        Ok("native persistent X11/EWMH connection is available".to_string())
    })
}

pub(super) fn list_windows() -> Result<Vec<WindowInfo>> {
    with_session(NativeSession::list_windows)
}

pub(super) fn focused_window() -> Result<Option<WindowInfo>> {
    with_session(NativeSession::focused_window)
}

pub(super) fn activate_window(window_id: u64) -> Result<()> {
    let window_id = u32::try_from(window_id).context("X11 window id exceeds 32 bits")?;
    with_session(|session| session.activate_window(window_id))
}

fn with_session<T>(operation: impl FnOnce(&mut NativeSession) -> Result<T>) -> Result<T> {
    let display = env::var("DISPLAY").context("DISPLAY is unset")?;
    let mut guard = native_session();
    if guard
        .as_ref()
        .is_none_or(|session| session.display != display)
    {
        *guard = Some(NativeSession::connect(display)?);
    }
    let result = operation(guard.as_mut().expect("native X11 session initialized"));
    if result.is_err() {
        *guard = None;
    }
    result
}

fn native_session() -> MutexGuard<'static, Option<NativeSession>> {
    static SESSION: OnceLock<Mutex<Option<NativeSession>>> = OnceLock::new();
    SESSION
        .get_or_init(|| Mutex::new(None))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl NativeSession {
    fn connect(display: String) -> Result<Self> {
        let (connection, screen_index) = x11rb::connect(Some(display.as_str()))
            .with_context(|| format!("failed to connect to X11 display {display}"))?;
        let root = connection
            .setup()
            .roots
            .get(screen_index)
            .context("X11 display did not expose its selected screen")?
            .root;
        let atoms = Atoms::intern(&connection)?;
        connection
            .change_window_attributes(
                root,
                &ChangeWindowAttributesAux::new()
                    .event_mask(EventMask::PROPERTY_CHANGE | EventMask::SUBSTRUCTURE_NOTIFY),
            )?
            .check()
            .context("failed to subscribe to X11 root-window changes")?;
        connection.flush()?;
        Ok(Self {
            connection,
            display,
            root,
            atoms,
            dirty: true,
            snapshot: None,
        })
    }

    fn require_ewmh_support(&self) -> Result<()> {
        let supported = self.property_u32(self.root, self.atoms.net_supported, AtomEnum::ATOM)?;
        if !supported.contains(&self.atoms.client_list)
            && !supported.contains(&self.atoms.client_list_stacking)
        {
            bail!("window manager does not advertise an EWMH client list");
        }
        if !supported.contains(&self.atoms.active_window) {
            bail!("window manager does not advertise _NET_ACTIVE_WINDOW");
        }
        Ok(())
    }

    fn drain_events(&mut self) -> Result<()> {
        while let Some(event) = self.connection.poll_for_event()? {
            match event {
                Event::PropertyNotify(_)
                | Event::CreateNotify(_)
                | Event::DestroyNotify(_)
                | Event::ReparentNotify(_)
                | Event::ConfigureNotify(_)
                | Event::MapNotify(_)
                | Event::UnmapNotify(_) => self.dirty = true,
                _ => {}
            }
        }
        Ok(())
    }

    fn list_windows(&mut self) -> Result<Vec<WindowInfo>> {
        self.drain_events()?;
        let now = Instant::now();
        let cached_windows = (!self.dirty)
            .then_some(self.snapshot.as_ref())
            .flatten()
            .filter(|snapshot| now.saturating_duration_since(snapshot.captured_at) < SNAPSHOT_TTL)
            .map(|snapshot| snapshot.windows.clone());
        if let Some(windows) = cached_windows {
            return Ok(windows);
        }
        self.require_ewmh_support()?;
        let active = self.active_window_id()?;
        let mut window_ids =
            self.property_u32(self.root, self.atoms.client_list_stacking, AtomEnum::WINDOW)?;
        if window_ids.is_empty() {
            window_ids = self.property_u32(self.root, self.atoms.client_list, AtomEnum::WINDOW)?;
        }
        let window_count = window_ids.len();
        let mut windows = window_ids
            .into_iter()
            .filter_map(|window| self.window_info(window, active).ok())
            .collect::<Vec<_>>();
        if window_count != 0 && windows.is_empty() {
            bail!("native X11 client list contained windows, but none could be inspected");
        }
        windows.sort_by_key(|window| window.window_id);
        self.snapshot = Some(CachedSnapshot {
            captured_at: now,
            windows: windows.clone(),
        });
        self.dirty = false;
        Ok(windows)
    }

    fn focused_window(&mut self) -> Result<Option<WindowInfo>> {
        self.drain_events()?;
        let Some(active) = self.active_window_id()? else {
            return Ok(None);
        };
        let cached_window = (!self.dirty)
            .then_some(self.snapshot.as_ref())
            .flatten()
            .and_then(|snapshot| {
                snapshot
                    .windows
                    .iter()
                    .find(|window| window.window_id == u64::from(active))
            });
        if let Some(window) = cached_window {
            let mut window = window.clone();
            window.focused = true;
            return Ok(Some(window));
        }
        self.window_info(active, Some(active)).map(Some)
    }

    fn activate_window(&mut self, window: Window) -> Result<()> {
        self.require_ewmh_support()?;
        let mut clients = self.property_u32(self.root, self.atoms.client_list, AtomEnum::WINDOW)?;
        if clients.is_empty() {
            clients =
                self.property_u32(self.root, self.atoms.client_list_stacking, AtomEnum::WINDOW)?;
        }
        if !clients.contains(&window) {
            bail!("X11 window 0x{window:08x} is not in the current EWMH client list");
        }
        let event = ClientMessageEvent::new(
            32,
            window,
            self.atoms.active_window,
            ClientMessageData::from([2, CURRENT_TIME, 0, 0, 0]),
        );
        self.connection
            .send_event(
                false,
                self.root,
                EventMask::SUBSTRUCTURE_REDIRECT | EventMask::SUBSTRUCTURE_NOTIFY,
                event,
            )?
            .check()
            .context("window manager rejected _NET_ACTIVE_WINDOW")?;
        self.connection.flush()?;
        self.dirty = true;
        Ok(())
    }

    fn active_window_id(&self) -> Result<Option<Window>> {
        Ok(self
            .property_u32(self.root, self.atoms.active_window, AtomEnum::WINDOW)?
            .into_iter()
            .next()
            .filter(|window| *window != 0))
    }

    fn window_info(&self, window: Window, active: Option<Window>) -> Result<WindowInfo> {
        self.connection
            .change_window_attributes(
                window,
                &ChangeWindowAttributesAux::new()
                    .event_mask(EventMask::PROPERTY_CHANGE | EventMask::STRUCTURE_NOTIFY),
            )?
            .check()?;
        let geometry = self.connection.get_geometry(window)?.reply()?;
        let translated = self
            .connection
            .translate_coordinates(window, self.root, 0, 0)?
            .reply()?;
        let title = self
            .property_bytes(window, self.atoms.net_wm_name, self.atoms.utf8_string)?
            .and_then(clean_bytes)
            .or_else(|| {
                self.property_bytes(window, AtomEnum::WM_NAME.into(), AtomEnum::ANY)
                    .ok()
                    .flatten()
                    .and_then(clean_bytes)
            });
        let (app_id, wm_class) = self
            .property_bytes(window, AtomEnum::WM_CLASS.into(), AtomEnum::STRING)?
            .map(|value| parse_wm_class(&value))
            .unwrap_or_default();
        let pid = self
            .property_u32(window, self.atoms.net_wm_pid, AtomEnum::CARDINAL)?
            .into_iter()
            .next()
            .filter(|pid| *pid != 0);
        let workspace = self
            .property_u32(window, self.atoms.net_wm_desktop, AtomEnum::CARDINAL)?
            .into_iter()
            .next()
            .and_then(|desktop| i32::try_from(desktop).ok());
        let states = self.property_u32(window, self.atoms.net_wm_state, AtomEnum::ATOM)?;
        Ok(WindowInfo {
            window_id: u64::from(window),
            title,
            app_id,
            wm_class,
            pid,
            bounds: Some(WindowBounds {
                x: Some(i32::from(translated.dst_x)),
                y: Some(i32::from(translated.dst_y)),
                width: u32::from(geometry.width),
                height: u32::from(geometry.height),
            }),
            workspace,
            focused: active == Some(window),
            hidden: states.contains(&self.atoms.net_wm_state_hidden),
            client_type: Some("x11".to_string()),
            backend: X11_BACKEND.to_string(),
            terminal: None,
        })
    }

    fn property_u32<T: Into<Atom>>(
        &self,
        window: Window,
        property: Atom,
        property_type: T,
    ) -> Result<Vec<u32>> {
        let reply = self
            .connection
            .get_property(
                false,
                window,
                property,
                property_type,
                0,
                MAX_PROPERTY_LONGS,
            )?
            .reply()?;
        Ok(reply.value32().map(Iterator::collect).unwrap_or_default())
    }

    fn property_bytes<T: Into<Atom>>(
        &self,
        window: Window,
        property: Atom,
        property_type: T,
    ) -> Result<Option<Vec<u8>>> {
        let reply = self
            .connection
            .get_property(
                false,
                window,
                property,
                property_type,
                0,
                MAX_PROPERTY_LONGS,
            )?
            .reply()?;
        Ok((reply.type_ != u32::from(AtomEnum::NONE)).then_some(reply.value))
    }
}

impl Atoms {
    fn intern(connection: &RustConnection) -> Result<Self> {
        fn atom(connection: &RustConnection, name: &[u8]) -> Result<Atom> {
            Ok(connection.intern_atom(false, name)?.reply()?.atom)
        }
        Ok(Self {
            active_window: atom(connection, b"_NET_ACTIVE_WINDOW")?,
            client_list: atom(connection, b"_NET_CLIENT_LIST")?,
            client_list_stacking: atom(connection, b"_NET_CLIENT_LIST_STACKING")?,
            net_supported: atom(connection, b"_NET_SUPPORTED")?,
            net_wm_desktop: atom(connection, b"_NET_WM_DESKTOP")?,
            net_wm_name: atom(connection, b"_NET_WM_NAME")?,
            net_wm_pid: atom(connection, b"_NET_WM_PID")?,
            net_wm_state: atom(connection, b"_NET_WM_STATE")?,
            net_wm_state_hidden: atom(connection, b"_NET_WM_STATE_HIDDEN")?,
            utf8_string: atom(connection, b"UTF8_STRING")?,
        })
    }
}

fn clean_bytes(value: Vec<u8>) -> Option<String> {
    let value = String::from_utf8_lossy(&value)
        .trim_matches('\0')
        .trim()
        .to_string();
    (!value.is_empty()).then_some(value)
}

fn parse_wm_class(value: &[u8]) -> (Option<String>, Option<String>) {
    let mut fields = value
        .split(|byte| *byte == 0)
        .map(|field| String::from_utf8_lossy(field).trim().to_string())
        .filter(|field| !field.is_empty());
    let instance = fields.next();
    let class = fields.next().or_else(|| instance.clone());
    (instance, class)
}

#[cfg(test)]
#[path = "x11_native_tests.rs"]
mod tests;
