use crate::cosmic_helper;
use crate::screenshot::RawScreenshotCapture;
use crate::screenshot_impl::{read_png_as_capture_inner, temp_png_path};
use crate::windowing::backends::hyprland;
use crate::windowing::{WindowInfo, COSMIC_WAYLAND_BACKEND, HYPRLAND_BACKEND};
use anyhow::{bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::process::Command;
use tokio::sync::OnceCell;

const EXACT_CAPTURE_TIMEOUT: Duration = Duration::from_secs(20);
const EXACT_CAPTURE_PROBE_TIMEOUT: Duration = Duration::from_secs(2);
static GRIM_EXACT_CAPTURE: OnceCell<bool> = OnceCell::const_new();

/// Capture exact window-local pixels, returning `None` when the runtime lacks
/// compositor-native support. Once support is confirmed, capture failures are
/// returned rather than falling back to desktop pixels.
pub(crate) async fn capture_window_exact(
    window: &WindowInfo,
) -> Result<Option<RawScreenshotCapture>> {
    if window.backend == COSMIC_WAYLAND_BACKEND {
        let window_id = window.window_id;
        let temporary = TemporaryCapture::new(temp_png_path("cosmic-window"));
        let path = temporary.path().to_path_buf();
        let capture_path = path.clone();
        tokio::task::spawn_blocking(move || {
            cosmic_helper::capture_window(window_id, &capture_path)
        })
        .await
        .context("COSMIC exact capture task failed")??;
        let capture = tokio::task::spawn_blocking(move || {
            read_png_as_capture_inner(&path, "cosmic-ext-image-copy-exact")
        })
        .await
        .context("exact screenshot decode task failed")??;
        return Ok(Some(capture));
    }

    if window.backend != HYPRLAND_BACKEND || !grim_supports_exact_capture().await {
        return Ok(None);
    }

    let window = window.clone();
    let capture_id = tokio::task::spawn_blocking(move || hyprland::exact_capture_id(&window))
        .await
        .context("Hyprland capture-ID resolution task failed")??;
    let Some(capture_id) = capture_id else {
        return Ok(None);
    };
    let temporary = TemporaryCapture::new(temp_png_path("hyprland-window"));
    let mut command = Command::new("grim");
    command
        .arg("-T")
        .arg(capture_id)
        .arg(temporary.path())
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true);
    let output = tokio::time::timeout(EXACT_CAPTURE_TIMEOUT, command.output())
        .await
        .context("grim exact window capture timed out")?
        .context("failed to run grim for exact Hyprland window capture")?;
    if !output.status.success() {
        bail!(
            "grim exact Hyprland window capture failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let path = temporary.path().to_path_buf();
    let capture = tokio::task::spawn_blocking(move || {
        read_png_as_capture_inner(&path, "hyprland-grim-exact")
    })
    .await
    .context("exact screenshot decode task failed")??;
    Ok(Some(capture))
}

async fn grim_supports_exact_capture() -> bool {
    *GRIM_EXACT_CAPTURE
        .get_or_init(|| async {
            let mut command = Command::new("grim");
            command
                .arg("-h")
                .stdin(std::process::Stdio::null())
                .kill_on_drop(true);
            tokio::time::timeout(EXACT_CAPTURE_PROBE_TIMEOUT, command.output())
                .await
                .ok()
                .and_then(Result::ok)
                .is_some_and(|output| grim_help_supports_exact(&output.stdout, &output.stderr))
        })
        .await
}

fn grim_help_supports_exact(stdout: &[u8], stderr: &[u8]) -> bool {
    stdout.windows(2).any(|value| value == b"-T") || stderr.windows(2).any(|value| value == b"-T")
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
#[path = "capture_tests.rs"]
mod tests;
