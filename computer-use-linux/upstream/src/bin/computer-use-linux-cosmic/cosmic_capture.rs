use super::{stable_window_id, AppData};
use anyhow::{anyhow, bail, Context, Result};
use polling::{Event, Events, Poller};
use std::os::unix::io::{AsFd, BorrowedFd};
use std::path::Path;
use std::time::{Duration, Instant};
use wayland_client::globals::GlobalList;
use wayland_client::protocol::{wl_buffer, wl_output, wl_shm, wl_shm_pool};
use wayland_client::{Connection, Dispatch, EventQueue, Proxy, QueueHandle, WEnum};
use wayland_protocols::ext::image_capture_source::v1::client::{
    ext_foreign_toplevel_image_capture_source_manager_v1, ext_image_capture_source_v1,
};
use wayland_protocols::ext::image_copy_capture::v1::client::{
    ext_image_copy_capture_frame_v1, ext_image_copy_capture_manager_v1,
    ext_image_copy_capture_session_v1,
};

#[path = "cosmic_capture/output.rs"]
mod output;

use output::{write_png, BackingFile};

const MAX_CAPTURE_BYTES: u64 = 256 * 1024 * 1024;
const CAPTURE_TIMEOUT: Duration = Duration::from_secs(4);

#[derive(Default)]
pub(super) struct CaptureRuntime {
    shm: Option<wl_shm::WlShm>,
    source_manager: Option<
        ext_foreign_toplevel_image_capture_source_manager_v1::ExtForeignToplevelImageCaptureSourceManagerV1,
    >,
    capture_manager:
        Option<ext_image_copy_capture_manager_v1::ExtImageCopyCaptureManagerV1>,
    state: CaptureState,
}

impl CaptureRuntime {
    pub(super) fn bind_initial(&mut self, globals: &GlobalList, qh: &QueueHandle<AppData>) {
        self.shm = globals.bind::<wl_shm::WlShm, _, _>(qh, 1..=1, ()).ok();
        self.source_manager = globals
            .bind::<
                ext_foreign_toplevel_image_capture_source_manager_v1::ExtForeignToplevelImageCaptureSourceManagerV1,
                _,
                _,
            >(qh, 1..=1, ())
            .ok();
        self.capture_manager = globals
            .bind::<ext_image_copy_capture_manager_v1::ExtImageCopyCaptureManagerV1, _, _>(
                qh,
                1..=1,
                (),
            )
            .ok();
    }
}

#[derive(Default)]
struct CaptureState {
    width: Option<u32>,
    height: Option<u32>,
    shm_formats: Vec<WEnum<wl_shm::Format>>,
    constraints_done: bool,
    stopped: bool,
    frame: FrameState,
    transform: Option<WEnum<wl_output::Transform>>,
}

#[derive(Default)]
enum FrameState {
    #[default]
    Pending,
    Ready,
    Failed(String),
}

pub(super) fn capture_window(
    event_queue: &mut EventQueue<AppData>,
    app_data: &mut AppData,
    window_id: u64,
    output_path: &Path,
) -> Result<()> {
    let mut matching = app_data.records.values().filter(|record| {
        record
            .identifier
            .as_deref()
            .is_some_and(|identifier| stable_window_id(identifier) == window_id)
    });
    let record = matching
        .next()
        .ok_or_else(|| anyhow!("no live COSMIC toplevel matched window_id {window_id}"))?;
    if matching.next().is_some() {
        bail!("window_id {window_id} ambiguously matched multiple COSMIC toplevels");
    }
    if !record.cosmic_state_known {
        bail!("COSMIC exact capture rejected window_id {window_id} with unknown compositor state");
    }
    if !record.cosmic.as_ref().is_some_and(Proxy::is_alive) {
        bail!("COSMIC exact capture rejected window_id {window_id} without a live COSMIC handle");
    }
    if record.hidden {
        bail!("COSMIC exact capture rejected minimized window_id {window_id}");
    }
    let foreign = record
        .foreign
        .clone()
        .ok_or_else(|| anyhow!("COSMIC toplevel {window_id} lost its foreign handle"))?;
    if !foreign.is_alive() {
        bail!("COSMIC exact capture rejected stale foreign handle for window_id {window_id}");
    }
    let shm = app_data
        .capture
        .shm
        .clone()
        .ok_or_else(|| anyhow!("COSMIC exact capture requires wl_shm"))?;
    let source_manager = app_data.capture.source_manager.clone().ok_or_else(|| {
        anyhow!("COSMIC exact capture source protocol is unavailable in this session")
    })?;
    let capture_manager = app_data.capture.capture_manager.clone().ok_or_else(|| {
        anyhow!("COSMIC image-copy capture protocol is unavailable in this session")
    })?;

    app_data.capture.state = CaptureState::default();
    let qh = event_queue.handle();
    let source = source_manager.create_source(&foreign, &qh, ());
    let session = capture_manager.create_session(
        &source,
        ext_image_copy_capture_manager_v1::Options::empty(),
        &qh,
        (),
    );
    let deadline = Instant::now() + CAPTURE_TIMEOUT;

    let result = capture_session(
        event_queue,
        app_data,
        &qh,
        &shm,
        &session,
        output_path,
        deadline,
    );
    session.destroy();
    source.destroy();
    result
}

fn capture_session(
    event_queue: &mut EventQueue<AppData>,
    app_data: &mut AppData,
    qh: &QueueHandle<AppData>,
    shm: &wl_shm::WlShm,
    session: &ext_image_copy_capture_session_v1::ExtImageCopyCaptureSessionV1,
    output_path: &Path,
    deadline: Instant,
) -> Result<()> {
    let negotiated = dispatch_until(event_queue, app_data, deadline, |state| {
        state.constraints_done || state.stopped
    })?;
    if app_data.capture.state.stopped {
        bail!("COSMIC stopped the exact window capture session");
    }
    if !negotiated || !app_data.capture.state.constraints_done {
        bail!("COSMIC exact capture buffer negotiation timed out");
    }

    let width = app_data
        .capture
        .state
        .width
        .ok_or_else(|| anyhow!("COSMIC capture constraints omitted the buffer width"))?;
    let height = app_data
        .capture
        .state
        .height
        .ok_or_else(|| anyhow!("COSMIC capture constraints omitted the buffer height"))?;
    let format = select_shm_format(&app_data.capture.state.shm_formats)?;
    let layout = BufferLayout::new(width, height)?;
    let mut backing = BackingFile::new(layout.byte_len)?;
    let pool = shm.create_pool(backing.file.as_fd(), layout.byte_len_i32, qh, ());
    let buffer = pool.create_buffer(
        0,
        layout.width_i32,
        layout.height_i32,
        layout.stride_i32,
        format,
        qh,
        (),
    );
    pool.destroy();
    let frame = session.create_frame(qh, ());
    frame.attach_buffer(&buffer);
    frame.damage_buffer(0, 0, layout.width_i32, layout.height_i32);
    frame.capture();

    let result = finish_frame(
        event_queue,
        app_data,
        &mut backing,
        layout,
        output_path,
        format,
        deadline,
    );
    frame.destroy();
    buffer.destroy();
    result
}

fn finish_frame(
    event_queue: &mut EventQueue<AppData>,
    app_data: &mut AppData,
    backing: &mut BackingFile,
    layout: BufferLayout,
    output_path: &Path,
    format: wl_shm::Format,
    deadline: Instant,
) -> Result<()> {
    let completed = dispatch_until(event_queue, app_data, deadline, |state| {
        !matches!(state.frame, FrameState::Pending) || state.stopped
    })?;
    if !completed {
        bail!("COSMIC exact capture frame timed out");
    }

    match &app_data.capture.state.frame {
        FrameState::Ready => {
            ensure_normal_transform(app_data.capture.state.transform)?;
            let pixels = backing.read_all(layout.byte_len_usize)?;
            write_png(output_path, layout.width, layout.height, format, pixels)
        }
        FrameState::Failed(reason) => Err(anyhow!("COSMIC exact capture failed: {reason}")),
        FrameState::Pending if app_data.capture.state.stopped => Err(anyhow!(
            "COSMIC stopped the exact capture session before a frame was ready"
        )),
        FrameState::Pending => Err(anyhow!("COSMIC did not complete the exact capture frame")),
    }
}

fn dispatch_until(
    event_queue: &mut EventQueue<AppData>,
    app_data: &mut AppData,
    deadline: Instant,
    complete: impl Fn(&CaptureState) -> bool,
) -> Result<bool> {
    loop {
        event_queue
            .dispatch_pending(app_data)
            .context("failed to dispatch pending COSMIC capture events")?;
        if complete(&app_data.capture.state) {
            return Ok(true);
        }
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Ok(false);
        };
        event_queue
            .flush()
            .context("failed to flush COSMIC capture requests")?;
        let Some(read_guard) = event_queue.prepare_read() else {
            continue;
        };
        if !wait_readable(read_guard.connection_fd(), remaining)? {
            return Ok(false);
        }
        read_guard
            .read()
            .context("failed to read COSMIC capture events")?;
    }
}

fn wait_readable(fd: BorrowedFd<'_>, remaining: Duration) -> Result<bool> {
    let fd = fd
        .try_clone_to_owned()
        .context("failed to duplicate COSMIC Wayland socket")?;
    let poller = Poller::new().context("failed to create COSMIC Wayland poller")?;
    // SAFETY: the duplicated descriptor remains alive until it is explicitly
    // removed from the poller below.
    unsafe { poller.add(&fd, Event::readable(1)) }
        .context("failed to register COSMIC Wayland socket")?;
    let mut events = Events::new();
    let wait_result = poller.wait(&mut events, Some(remaining));
    let delete_result = poller.delete(&fd);
    let count = wait_result.context("failed to poll COSMIC Wayland socket")?;
    delete_result.context("failed to unregister COSMIC Wayland socket")?;
    Ok(count > 0)
}

#[derive(Clone, Copy)]
struct BufferLayout {
    width: u32,
    height: u32,
    width_i32: i32,
    height_i32: i32,
    stride_i32: i32,
    byte_len_i32: i32,
    byte_len_usize: usize,
    byte_len: u64,
}

impl BufferLayout {
    fn new(width: u32, height: u32) -> Result<Self> {
        if width == 0 || height == 0 {
            bail!("COSMIC advertised an empty {width}x{height} capture buffer");
        }
        let stride = width
            .checked_mul(4)
            .ok_or_else(|| anyhow!("COSMIC capture stride overflow for width {width}"))?;
        let byte_len = u64::from(stride)
            .checked_mul(u64::from(height))
            .ok_or_else(|| anyhow!("COSMIC capture buffer size overflow"))?;
        if byte_len > MAX_CAPTURE_BYTES {
            bail!("COSMIC capture buffer requires {byte_len} bytes, exceeding the {MAX_CAPTURE_BYTES}-byte safety limit");
        }
        Ok(Self {
            width,
            height,
            width_i32: i32::try_from(width)
                .context("COSMIC capture width exceeds wl_shm limits")?,
            height_i32: i32::try_from(height)
                .context("COSMIC capture height exceeds wl_shm limits")?,
            stride_i32: i32::try_from(stride)
                .context("COSMIC capture stride exceeds wl_shm limits")?,
            byte_len_i32: i32::try_from(byte_len)
                .context("COSMIC capture size exceeds wl_shm limits")?,
            byte_len_usize: usize::try_from(byte_len)
                .context("COSMIC capture size exceeds addressable memory")?,
            byte_len,
        })
    }
}

fn select_shm_format(formats: &[WEnum<wl_shm::Format>]) -> Result<wl_shm::Format> {
    for preferred in [wl_shm::Format::Abgr8888, wl_shm::Format::Xbgr8888] {
        if formats
            .iter()
            .any(|format| matches!(format, WEnum::Value(value) if *value == preferred))
        {
            return Ok(preferred);
        }
    }
    bail!("COSMIC did not advertise a supported 8-bit RGBA wl_shm capture format")
}

fn ensure_normal_transform(transform: Option<WEnum<wl_output::Transform>>) -> Result<()> {
    match transform {
        Some(WEnum::Value(wl_output::Transform::Normal)) => Ok(()),
        Some(value) => bail!("COSMIC returned unsupported capture transform {value:?}"),
        None => bail!("COSMIC exact capture frame omitted its buffer transform"),
    }
}

impl Dispatch<ext_image_copy_capture_session_v1::ExtImageCopyCaptureSessionV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        _session: &ext_image_copy_capture_session_v1::ExtImageCopyCaptureSessionV1,
        event: ext_image_copy_capture_session_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            ext_image_copy_capture_session_v1::Event::BufferSize { width, height } => {
                app_data.capture.state.width = Some(width);
                app_data.capture.state.height = Some(height);
            }
            ext_image_copy_capture_session_v1::Event::ShmFormat { format } => {
                app_data.capture.state.shm_formats.push(format);
            }
            ext_image_copy_capture_session_v1::Event::Done => {
                app_data.capture.state.constraints_done = true;
            }
            ext_image_copy_capture_session_v1::Event::Stopped => {
                app_data.capture.state.stopped = true;
            }
            ext_image_copy_capture_session_v1::Event::DmabufDevice { .. }
            | ext_image_copy_capture_session_v1::Event::DmabufFormat { .. }
            | _ => {}
        }
    }
}

impl Dispatch<ext_image_copy_capture_frame_v1::ExtImageCopyCaptureFrameV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        _frame: &ext_image_copy_capture_frame_v1::ExtImageCopyCaptureFrameV1,
        event: ext_image_copy_capture_frame_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            ext_image_copy_capture_frame_v1::Event::Transform { transform } => {
                app_data.capture.state.transform = Some(transform);
            }
            ext_image_copy_capture_frame_v1::Event::Ready => {
                app_data.capture.state.frame = FrameState::Ready;
            }
            ext_image_copy_capture_frame_v1::Event::Failed { reason } => {
                app_data.capture.state.frame = FrameState::Failed(format!("{reason:?}"));
            }
            ext_image_copy_capture_frame_v1::Event::Damage { .. }
            | ext_image_copy_capture_frame_v1::Event::PresentationTime { .. }
            | _ => {}
        }
    }
}

macro_rules! ignore_events {
    ($interface:path) => {
        impl Dispatch<$interface, ()> for AppData {
            fn event(
                _: &mut Self,
                _: &$interface,
                _: <$interface as wayland_client::Proxy>::Event,
                _: &(),
                _: &Connection,
                _: &QueueHandle<Self>,
            ) {
            }
        }
    };
}

ignore_events!(wl_shm::WlShm);
ignore_events!(wl_shm_pool::WlShmPool);
ignore_events!(wl_buffer::WlBuffer);
ignore_events!(
    ext_foreign_toplevel_image_capture_source_manager_v1::ExtForeignToplevelImageCaptureSourceManagerV1
);
ignore_events!(ext_image_capture_source_v1::ExtImageCaptureSourceV1);
ignore_events!(ext_image_copy_capture_manager_v1::ExtImageCopyCaptureManagerV1);

#[cfg(test)]
#[path = "cosmic_capture_tests.rs"]
mod tests;
