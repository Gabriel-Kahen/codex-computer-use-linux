use super::niri::{self, NIRI_BACKEND};
use crate::diagnostics::hydrate_session_bus_env;
use crate::screenshot::RawScreenshotCapture;
use crate::screenshot_impl::{read_png_as_capture_inner, temp_png_path};
use crate::windowing::WindowInfo;
use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::process::Command;
use zbus::zvariant::{OwnedObjectPath, Value};
use zbus::{Connection, Proxy};

const MUTTER_SCREENCAST_SERVICE: &str = "org.gnome.Mutter.ScreenCast";
const MUTTER_SCREENCAST_PATH: &str = "/org/gnome/Mutter/ScreenCast";
const MUTTER_SCREENCAST_INTERFACE: &str = "org.gnome.Mutter.ScreenCast";
const MUTTER_SCREENCAST_SESSION_INTERFACE: &str = "org.gnome.Mutter.ScreenCast.Session";
const MUTTER_SCREENCAST_STREAM_INTERFACE: &str = "org.gnome.Mutter.ScreenCast.Stream";
const STREAM_START_TIMEOUT: Duration = Duration::from_secs(10);
const FRAME_CAPTURE_TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Clone, Debug)]
pub(crate) struct ExactCaptureSupport {
    pub(crate) available: bool,
    pub(crate) detail: String,
}

pub(crate) fn exact_capture_support() -> ExactCaptureSupport {
    static SUPPORT: OnceLock<ExactCaptureSupport> = OnceLock::new();
    SUPPORT
        .get_or_init(|| {
            let Some(gst_launch) = command_path("gst-launch-1.0") else {
                return ExactCaptureSupport {
                    available: false,
                    detail: "exact inactive capture needs gst-launch-1.0".to_string(),
                };
            };
            let Some(gst_inspect) = command_path("gst-inspect-1.0") else {
                return ExactCaptureSupport {
                    available: false,
                    detail: "exact inactive capture needs gst-inspect-1.0 for a fail-closed plugin probe"
                        .to_string(),
                };
            };
            for plugin in ["pipewiresrc", "videoconvert", "pngenc"] {
                let present = std::process::Command::new(&gst_inspect)
                    .arg(plugin)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .is_ok_and(|status| status.success());
                if !present {
                    return ExactCaptureSupport {
                        available: false,
                        detail: format!(
                            "exact inactive capture needs the GStreamer {plugin} plugin"
                        ),
                    };
                }
            }
            ExactCaptureSupport {
                available: true,
                detail: format!(
                    "GStreamer exact-capture prerequisites are available at {}; Niri's stable window-ID ScreenCast service is verified for every capture",
                    gst_launch.display()
                ),
            }
        })
        .clone()
}

pub(crate) async fn capture_window_exact(window: &WindowInfo) -> Result<RawScreenshotCapture> {
    let support = exact_capture_support();
    if !support.available {
        bail!(
            "Niri exact inactive window capture is unavailable: {}. Refusing desktop or monitor substitution",
            support.detail
        );
    }

    let expected = window.clone();
    let window_id = tokio::task::spawn_blocking(move || validate_capture_target(&expected))
        .await
        .context("Niri capture target validation task failed")??;
    hydrate_session_bus_env();
    let connection = Connection::session()
        .await
        .context("failed to connect to the session bus for Niri exact capture")?;
    let session_path = create_session(&connection).await?;
    let result = capture_session_window(&connection, &session_path, window_id).await;
    let stop_result = stop_session(&connection, &session_path).await;
    let capture = match (result, stop_result) {
        (Ok(capture), Ok(())) => capture,
        (Ok(_), Err(error)) => {
            return Err(error).context("captured Niri window but failed to stop ScreenCast session")
        }
        (Err(error), _) => return Err(error),
    };

    let expected = window.clone();
    tokio::task::spawn_blocking(move || validate_capture_target(&expected))
        .await
        .context("Niri post-capture identity validation task failed")??;
    Ok(capture)
}

async fn create_session(connection: &Connection) -> Result<OwnedObjectPath> {
    let proxy = Proxy::new(
        connection,
        MUTTER_SCREENCAST_SERVICE,
        MUTTER_SCREENCAST_PATH,
        MUTTER_SCREENCAST_INTERFACE,
    )
    .await
    .context("Niri's org.gnome.Mutter.ScreenCast interface is unavailable")?;
    let properties: HashMap<&str, Value<'_>> = HashMap::new();
    proxy
        .call("CreateSession", &properties)
        .await
        .context("Niri ScreenCast CreateSession failed")
}

async fn capture_session_window(
    connection: &Connection,
    session_path: &OwnedObjectPath,
    window_id: u64,
) -> Result<RawScreenshotCapture> {
    let session = Proxy::new(
        connection,
        MUTTER_SCREENCAST_SERVICE,
        session_path.as_str(),
        MUTTER_SCREENCAST_SESSION_INTERFACE,
    )
    .await
    .context("failed to create Niri ScreenCast session proxy")?;
    let mut properties: HashMap<&str, Value<'_>> = HashMap::new();
    properties.insert("window-id", Value::from(window_id));
    let stream_path: OwnedObjectPath = session
        .call("RecordWindow", &properties)
        .await
        .with_context(|| format!("Niri refused ScreenCast window id {window_id}"))?;
    let stream = Proxy::new(
        connection,
        MUTTER_SCREENCAST_SERVICE,
        stream_path.as_str(),
        MUTTER_SCREENCAST_STREAM_INTERFACE,
    )
    .await
    .context("failed to create Niri ScreenCast stream proxy")?;
    let mut node_signals = stream
        .receive_signal("PipeWireStreamAdded")
        .await
        .context("failed to subscribe to Niri's PipeWire stream signal")?;
    session
        .call::<_, _, ()>("Start", &())
        .await
        .context("Niri ScreenCast session failed to start")?;
    let signal = tokio::time::timeout(STREAM_START_TIMEOUT, node_signals.next())
        .await
        .context("timed out waiting for Niri's exact window PipeWire node")?
        .context("Niri closed the exact window ScreenCast stream before publishing a node")?;
    let node_id: u32 = signal
        .body()
        .deserialize()
        .context("Niri returned an invalid PipeWire node id")?;
    capture_pipewire_frame(node_id).await
}

async fn stop_session(connection: &Connection, session_path: &OwnedObjectPath) -> Result<()> {
    let session = Proxy::new(
        connection,
        MUTTER_SCREENCAST_SERVICE,
        session_path.as_str(),
        MUTTER_SCREENCAST_SESSION_INTERFACE,
    )
    .await
    .context("failed to create Niri ScreenCast cleanup proxy")?;
    session
        .call::<_, _, ()>("Stop", &())
        .await
        .context("Niri ScreenCast Stop failed")
}

async fn capture_pipewire_frame(node_id: u32) -> Result<RawScreenshotCapture> {
    let temporary = TemporaryCapture::new(temp_png_path("niri-window"));
    let support = exact_capture_support();
    if !support.available {
        bail!("Niri exact capture transport disappeared during capture")
    }
    let gst_launch = command_path("gst-launch-1.0")
        .context("gst-launch-1.0 disappeared during Niri exact capture")?;
    let args = gstreamer_pipeline_args(node_id, temporary.path());
    let mut command = Command::new(gst_launch);
    command.args(args).stdin(Stdio::null()).kill_on_drop(true);
    let output = tokio::time::timeout(FRAME_CAPTURE_TIMEOUT, command.output())
        .await
        .context("Niri exact PipeWire frame capture timed out")?
        .context("failed to start GStreamer for Niri exact capture")?;
    if !output.status.success() {
        bail!(
            "Niri exact PipeWire frame capture failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let path = temporary.path().to_path_buf();
    tokio::task::spawn_blocking(move || read_png_as_capture_inner(&path, "niri-mutter-screencast"))
        .await
        .context("Niri exact screenshot decode task failed")?
}

fn validate_capture_target(expected: &WindowInfo) -> Result<u64> {
    if expected.backend != NIRI_BACKEND {
        bail!("exact Niri capture received a non-Niri window")
    }
    if expected.hidden {
        bail!("exact Niri capture refuses a minimized or hidden window")
    }
    let current = niri::list_windows()
        .context("failed to refresh Niri windows before exact capture")?
        .into_iter()
        .find(|window| window.window_id == expected.window_id);
    validate_current_window(expected, current.as_ref())
}

fn validate_current_window(expected: &WindowInfo, current: Option<&WindowInfo>) -> Result<u64> {
    let current = current.with_context(|| {
        format!(
            "Niri window {} is stale or closed; refusing capture substitution",
            expected.window_id
        )
    })?;
    if current.hidden {
        bail!(
            "Niri window {} became minimized or hidden",
            expected.window_id
        )
    }
    if expected.pid.is_some() && current.pid != expected.pid {
        bail!(
            "Niri window {} changed process identity before exact capture",
            expected.window_id
        )
    }
    if expected.app_id.is_some() && current.app_id != expected.app_id {
        bail!(
            "Niri window {} changed application identity before exact capture",
            expected.window_id
        )
    }
    Ok(current.window_id)
}

fn gstreamer_pipeline_args(node_id: u32, output: &Path) -> Vec<OsString> {
    [
        "-q".into(),
        "-e".into(),
        "pipewiresrc".into(),
        format!("path={node_id}").into(),
        "do-timestamp=true".into(),
        "num-buffers=1".into(),
        "!".into(),
        "videoconvert".into(),
        "!".into(),
        "pngenc".into(),
        "snapshot=true".into(),
        "!".into(),
        "filesink".into(),
        format!("location={}", output.display()).into(),
    ]
    .into()
}

fn command_path(binary: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|entry| entry.join(binary))
        .find(|candidate| is_executable(candidate))
}

fn is_executable(path: &Path) -> bool {
    fs::metadata(path)
        .map(|metadata| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            }
            #[cfg(not(unix))]
            {
                metadata.is_file()
            }
        })
        .unwrap_or(false)
}

struct TemporaryCapture(PathBuf);

impl TemporaryCapture {
    fn new(path: PathBuf) -> Self {
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TemporaryCapture {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[cfg(test)]
#[path = "niri_capture_tests.rs"]
mod tests;
