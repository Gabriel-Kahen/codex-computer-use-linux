//! Persistent native X11/EWMH transport for the generic X11 backend.
//!
//! The connection stays open for the MCP process lifetime. Root and client
//! events invalidate the bounded snapshot cache; callers still get a fresh
//! `_NET_ACTIVE_WINDOW` read for focused-window verification.

use crate::windowing::backends::x11::X11_BACKEND;
use crate::windowing::types::{WindowBounds, WindowInfo};
use anyhow::{bail, Context, Result};
use image::codecs::png::PngEncoder;
use image::{ColorType, ImageEncoder};
use std::env;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};
use x11rb::connection::{Connection, RequestConnection};
use x11rb::errors::{ConnectionError, ReplyError, ReplyOrIdError};
use x11rb::image::{Image as X11Image, PixelLayout};
use x11rb::protocol::xproto::{
    Atom, AtomEnum, ChangeWindowAttributesAux, ClientMessageData, ClientMessageEvent,
    ConnectionExt, EventMask, MapState, Visualid, Visualtype, Window,
};
use x11rb::protocol::Event;
use x11rb::protocol::{composite, res};
use x11rb::rust_connection::RustConnection;
use x11rb::CURRENT_TIME;

const SNAPSHOT_TTL: Duration = Duration::from_millis(250);
// X11 expresses get_property's maximum reply size in four-byte units. Keep
// malformed or unexpectedly large root properties from growing without bound.
const MAX_PROPERTY_LONGS: u32 = 16 * 1024;
const MAX_CAPTURE_PIXELS: u64 = 7680 * 4320;
// A lossless RGB PNG can be slightly larger than its uncompressed scanlines.
// The shared screenshot pipeline applies its own model-payload resizing after
// exact capture, so keep this engine limit memory-safe without rejecting noisy
// ordinary windows merely because they do not compress below the MCP cap.
const MAX_CAPTURE_PNG_BYTES: usize = MAX_CAPTURE_PIXELS as usize * 3 + 1024 * 1024;

pub(crate) struct NativeCapture {
    pub(crate) png: Vec<u8>,
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) authenticated_pid: u32,
}

struct NativePixels {
    image: X11Image<'static>,
    layout: PixelLayout,
    width: u16,
    height: u16,
    authenticated_pid: u32,
}

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

#[derive(Default)]
struct SnapshotCache {
    dirty: bool,
    snapshot: Option<CachedSnapshot>,
}

#[derive(Debug, Eq, PartialEq)]
pub(super) enum NativeActivation {
    Activated,
    WindowNotManaged,
}

struct NativeSession {
    connection: RustConnection,
    display: String,
    screen_index: usize,
    root: Window,
    atoms: Atoms,
    cache: SnapshotCache,
    compositor_selection: Atom,
    composite_supported: Option<bool>,
    res_supported: Option<bool>,
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

pub(super) fn activate_window(window_id: u64) -> Result<NativeActivation> {
    let Ok(window_id) = u32::try_from(window_id) else {
        return Ok(NativeActivation::WindowNotManaged);
    };
    with_session(|session| session.activate_window(window_id))
}

pub(crate) fn authenticated_pid(window_id: u64) -> Result<u32> {
    let window_id = u32::try_from(window_id).context("X11 window id exceeds 32 bits")?;
    with_session(|session| session.authenticated_pid(window_id))
}

pub(crate) fn capture_window(
    window_id: u64,
    expected_pid: Option<u32>,
    expected_size: Option<(u32, u32)>,
) -> Result<Option<NativeCapture>> {
    let window_id = u32::try_from(window_id).context("X11 window id exceeds 32 bits")?;
    let pixels = with_session(|session| {
        session.capture_window_pixels(window_id, expected_pid, expected_size)
    })?;
    pixels.map(encode_capture).transpose()
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
    if result.as_ref().is_err_and(is_connection_failure) {
        *guard = None;
    }
    result
}

fn is_connection_failure(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause.is::<ConnectionError>()
            || cause
                .downcast_ref::<ReplyError>()
                .is_some_and(|error| matches!(error, ReplyError::ConnectionError(_)))
            || cause
                .downcast_ref::<ReplyOrIdError>()
                .is_some_and(|error| matches!(error, ReplyOrIdError::ConnectionError(_)))
    })
}

fn native_session() -> MutexGuard<'static, Option<NativeSession>> {
    static SESSION: OnceLock<Mutex<Option<NativeSession>>> = OnceLock::new();
    SESSION
        .get_or_init(|| Mutex::new(None))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn intern_atom(connection: &RustConnection, name: &[u8]) -> Result<Atom> {
    Ok(connection.intern_atom(false, name)?.reply()?.atom)
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
        let compositor_selection = intern_atom(
            &connection,
            format!("_NET_WM_CM_S{screen_index}").as_bytes(),
        )?;
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
            screen_index,
            root,
            atoms,
            cache: SnapshotCache::default(),
            compositor_selection,
            composite_supported: None,
            res_supported: None,
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
                | Event::UnmapNotify(_) => self.cache.invalidate(),
                _ => {}
            }
        }
        Ok(())
    }

    fn list_windows(&mut self) -> Result<Vec<WindowInfo>> {
        self.drain_events()?;
        let now = Instant::now();
        if let Some(windows) = self.cache.fresh_windows(now) {
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
        self.cache.replace(now, windows.clone());
        Ok(windows)
    }

    fn focused_window(&mut self) -> Result<Option<WindowInfo>> {
        self.drain_events()?;
        let Some(active) = self.active_window_id()? else {
            return Ok(None);
        };
        let cached_window = self.cache.window(active);
        if let Some(window) = cached_window {
            let mut window = window.clone();
            window.focused = true;
            return Ok(Some(window));
        }
        self.window_info(active, Some(active)).map(Some)
    }

    fn activate_window(&mut self, window: Window) -> Result<NativeActivation> {
        self.require_ewmh_support()?;
        let mut clients = self.property_u32(self.root, self.atoms.client_list, AtomEnum::WINDOW)?;
        if clients.is_empty() {
            clients =
                self.property_u32(self.root, self.atoms.client_list_stacking, AtomEnum::WINDOW)?;
        }
        if !clients.contains(&window) {
            return Ok(NativeActivation::WindowNotManaged);
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
        self.cache.invalidate();
        Ok(NativeActivation::Activated)
    }

    fn active_window_id(&self) -> Result<Option<Window>> {
        Ok(self
            .property_u32(self.root, self.atoms.active_window, AtomEnum::WINDOW)?
            .into_iter()
            .next()
            .filter(|window| *window != 0))
    }

    fn capture_window_pixels(
        &mut self,
        window: Window,
        expected_pid: Option<u32>,
        expected_size: Option<(u32, u32)>,
    ) -> Result<Option<NativePixels>> {
        if !self.composite_supported()? || !self.res_supported()? {
            return Ok(None);
        }
        if self
            .connection
            .get_selection_owner(self.compositor_selection)?
            .reply()?
            .owner
            == 0
        {
            return Ok(None);
        }
        if !self.client_windows()?.contains(&window) {
            bail!("X11 window 0x{window:08x} is not in the current EWMH client list");
        }
        let attributes = self.connection.get_window_attributes(window)?.reply()?;
        if attributes.map_state != MapState::VIEWABLE {
            bail!("X11 window 0x{window:08x} is not viewable; minimized windows cannot be captured exactly");
        }
        let geometry = self.connection.get_geometry(window)?.reply()?;
        let window_width = geometry.width;
        let window_height = geometry.height;
        let window_border = geometry.border_width;
        if let Some((expected_width, expected_height)) = expected_size {
            if u32::from(geometry.width) != expected_width
                || u32::from(geometry.height) != expected_height
            {
                bail!(
                    "X11 window 0x{window:08x} changed size before capture (expected {expected_width}x{expected_height}, found {}x{})",
                    geometry.width,
                    geometry.height
                );
            }
        }
        let authenticated_pid = self.authenticated_pid(window)?;
        if expected_pid.is_some_and(|expected_pid| expected_pid != authenticated_pid) {
            bail!("XRes PID identity changed before capture for X11 window 0x{window:08x}");
        }

        composite::redirect_window(&self.connection, window, composite::Redirect::AUTOMATIC)?
            .check()
            .context("failed to redirect X11 window for exact capture")?;
        // XComposite permits multiple clients to select automatic redirection;
        // unredirecting below releases only this connection's selection, not
        // the compositor's. Keep the cleanup explicit even for local ID
        // allocation failure so a healthy persistent connection never retains
        // our redirect accidentally.
        let pixmap = match self.connection.generate_id() {
            Ok(pixmap) => pixmap,
            Err(error) => {
                self.release_capture_redirect(window)
                    .context("failed to release X11 capture redirection after ID allocation")?;
                return Err(error.into());
            }
        };
        let capture_result = (|| -> Result<NativePixels> {
            composite::name_window_pixmap(&self.connection, window, pixmap)?
                .check()
                .context("the compositor did not expose a named X11 window pixmap")?;
            let geometry = self.connection.get_geometry(pixmap)?.reply()?;
            let pixmap_width = u32::from(window_width) + u32::from(window_border) * 2;
            let pixmap_height = u32::from(window_height) + u32::from(window_border) * 2;
            if u32::from(geometry.width) != pixmap_width
                || u32::from(geometry.height) != pixmap_height
            {
                bail!(
                    "X11 window pixmap changed size during capture (expected {pixmap_width}x{pixmap_height}, found {}x{})",
                    geometry.width,
                    geometry.height
                );
            }
            let pixel_count = u64::from(window_width) * u64::from(window_height);
            if pixel_count == 0 || pixel_count > MAX_CAPTURE_PIXELS {
                bail!("X11 window pixmap exceeds the {MAX_CAPTURE_PIXELS}-pixel capture budget");
            }
            let layout = self.pixel_layout(attributes.visual)?;
            let image_offset = i16::try_from(window_border)
                .context("X11 window border is too wide to capture")?;
            let (image, _) = X11Image::get(
                &self.connection,
                pixmap,
                image_offset,
                image_offset,
                window_width,
                window_height,
            )?;
            let final_geometry = self.connection.get_geometry(window)?.reply()?;
            if final_geometry.width != window_width
                || final_geometry.height != window_height
                || final_geometry.border_width != window_border
            {
                bail!("X11 window bounds changed during capture for window 0x{window:08x}");
            }
            let final_pid = self.authenticated_pid(window)?;
            if final_pid != authenticated_pid {
                bail!("XRes PID identity changed during capture for X11 window 0x{window:08x}");
            }
            Ok(NativePixels {
                image: image.into_owned(),
                layout,
                width: window_width,
                height: window_height,
                authenticated_pid,
            })
        })();
        let free_result = (|| -> Result<()> {
            self.connection.free_pixmap(pixmap)?.check()?;
            Ok(())
        })();
        let unredirect_result = self.release_capture_redirect(window);
        let flush_result = self.connection.flush();
        let pixels = capture_result?;
        free_result.context("failed to release X11 capture pixmap")?;
        unredirect_result.context("failed to release X11 capture redirection")?;
        flush_result.context("failed to flush X11 capture cleanup")?;
        Ok(Some(pixels))
    }

    fn release_capture_redirect(&self, window: Window) -> Result<()> {
        composite::unredirect_window(&self.connection, window, composite::Redirect::AUTOMATIC)?
            .check()?;
        Ok(())
    }

    fn composite_supported(&mut self) -> Result<bool> {
        if self.composite_supported.is_none() {
            let supported = if self
                .connection
                .extension_information(composite::X11_EXTENSION_NAME)?
                .is_some()
            {
                let version = composite::query_version(&self.connection, 0, 2)?.reply()?;
                version.major_version > 0 || version.minor_version >= 2
            } else {
                false
            };
            self.composite_supported = Some(supported);
        }
        Ok(self.composite_supported == Some(true))
    }

    fn res_supported(&mut self) -> Result<bool> {
        if self.res_supported.is_none() {
            let supported = if self
                .connection
                .extension_information(res::X11_EXTENSION_NAME)?
                .is_some()
            {
                let version = res::query_version(&self.connection, 1, 2)?.reply()?;
                version.server_major > 1 || (version.server_major == 1 && version.server_minor >= 2)
            } else {
                false
            };
            self.res_supported = Some(supported);
        }
        Ok(self.res_supported == Some(true))
    }

    fn authenticated_pid(&mut self, window: Window) -> Result<u32> {
        if !self.res_supported()? {
            bail!("XRes 1.2 client PID authentication is unavailable");
        }
        let reply = res::query_client_ids(
            &self.connection,
            &[res::ClientIdSpec {
                client: window,
                mask: res::ClientIdMask::LOCAL_CLIENT_PID,
            }],
        )?
        .reply()?;
        reply
            .ids
            .into_iter()
            .flat_map(|value| value.value)
            .find(|pid| *pid != 0)
            .context("XRes did not authenticate a PID for this window")
    }

    fn client_windows(&self) -> Result<Vec<Window>> {
        let mut clients = self.property_u32(self.root, self.atoms.client_list, AtomEnum::WINDOW)?;
        if clients.is_empty() {
            clients =
                self.property_u32(self.root, self.atoms.client_list_stacking, AtomEnum::WINDOW)?;
        }
        Ok(clients)
    }

    fn pixel_layout(&self, visual_id: Visualid) -> Result<PixelLayout> {
        let visual = self
            .connection
            .setup()
            .roots
            .get(self.screen_index)
            .and_then(|screen| {
                screen
                    .allowed_depths
                    .iter()
                    .flat_map(|depth| depth.visuals.iter())
                    .find(|visual| visual.visual_id == visual_id)
            })
            .copied()
            .with_context(|| format!("X11 visual 0x{visual_id:08x} is unavailable"))?;
        pixel_layout(visual)
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

impl SnapshotCache {
    fn invalidate(&mut self) {
        self.dirty = true;
    }

    fn fresh_windows(&self, now: Instant) -> Option<Vec<WindowInfo>> {
        (!self.dirty)
            .then_some(self.snapshot.as_ref())
            .flatten()
            .filter(|snapshot| now.saturating_duration_since(snapshot.captured_at) < SNAPSHOT_TTL)
            .map(|snapshot| snapshot.windows.clone())
    }

    fn window(&self, window: Window) -> Option<&WindowInfo> {
        (!self.dirty)
            .then_some(self.snapshot.as_ref())
            .flatten()
            .and_then(|snapshot| {
                snapshot
                    .windows
                    .iter()
                    .find(|info| info.window_id == u64::from(window))
            })
    }

    fn replace(&mut self, captured_at: Instant, windows: Vec<WindowInfo>) {
        self.snapshot = Some(CachedSnapshot {
            captured_at,
            windows,
        });
        self.dirty = false;
    }
}

impl Atoms {
    fn intern(connection: &RustConnection) -> Result<Self> {
        Ok(Self {
            active_window: intern_atom(connection, b"_NET_ACTIVE_WINDOW")?,
            client_list: intern_atom(connection, b"_NET_CLIENT_LIST")?,
            client_list_stacking: intern_atom(connection, b"_NET_CLIENT_LIST_STACKING")?,
            net_supported: intern_atom(connection, b"_NET_SUPPORTED")?,
            net_wm_desktop: intern_atom(connection, b"_NET_WM_DESKTOP")?,
            net_wm_name: intern_atom(connection, b"_NET_WM_NAME")?,
            net_wm_pid: intern_atom(connection, b"_NET_WM_PID")?,
            net_wm_state: intern_atom(connection, b"_NET_WM_STATE")?,
            net_wm_state_hidden: intern_atom(connection, b"_NET_WM_STATE_HIDDEN")?,
            utf8_string: intern_atom(connection, b"UTF8_STRING")?,
        })
    }
}

fn pixel_layout(visual: Visualtype) -> Result<PixelLayout> {
    PixelLayout::from_visual_type(visual)
        .context("window does not use a supported direct-color visual")
}

fn encode_capture(pixels: NativePixels) -> Result<NativeCapture> {
    let pixel_count = usize::from(pixels.width) * usize::from(pixels.height);
    let mut rgb = Vec::with_capacity(pixel_count.saturating_mul(3));
    for y in 0..pixels.height {
        for x in 0..pixels.width {
            let (red, green, blue) = pixels.layout.decode(pixels.image.get_pixel(x, y));
            rgb.extend_from_slice(&[(red >> 8) as u8, (green >> 8) as u8, (blue >> 8) as u8]);
        }
    }
    let mut png = Vec::new();
    PngEncoder::new(&mut png).write_image(
        &rgb,
        u32::from(pixels.width),
        u32::from(pixels.height),
        ColorType::Rgb8.into(),
    )?;
    if png.len() > MAX_CAPTURE_PNG_BYTES {
        bail!(
            "exact X11 capture is {} bytes; maximum native capture size is {MAX_CAPTURE_PNG_BYTES} bytes",
            png.len()
        );
    }
    Ok(NativeCapture {
        png,
        width: u32::from(pixels.width),
        height: u32::from(pixels.height),
        authenticated_pid: pixels.authenticated_pid,
    })
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
