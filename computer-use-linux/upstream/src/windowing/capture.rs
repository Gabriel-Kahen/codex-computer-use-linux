use crate::diagnostics::hydrate_session_bus_env;
use crate::screenshot::RawScreenshotCapture;
use crate::screenshot_impl::{read_png_as_capture_inner, temp_png_path};
use crate::windowing::backends::{hyprland, kwin};
use crate::windowing::{WindowInfo, HYPRLAND_BACKEND, KWIN_BACKEND};
use anyhow::{bail, Context, Result};
use image::{DynamicImage, ImageFormat, Rgba, RgbaImage};
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{Cursor, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::process::Command;
use tokio::sync::OnceCell;
use zbus::zvariant::{Fd, OwnedValue, Value};
use zbus::{Connection, Proxy};

const EXACT_CAPTURE_TIMEOUT: Duration = Duration::from_secs(20);
const EXACT_CAPTURE_PROBE_TIMEOUT: Duration = Duration::from_secs(2);
const KWIN_CAPTURE_MAX_BYTES: usize = 256 * 1024 * 1024;
const KWIN_SCREENSHOT_SERVICE: &str = "org.kde.KWin.ScreenShot2";
const KWIN_SCREENSHOT_PATH: &str = "/org/kde/KWin/ScreenShot2";
const KWIN_SCREENSHOT_INTERFACE: &str = "org.kde.KWin.ScreenShot2";
static GRIM_EXACT_CAPTURE: OnceCell<bool> = OnceCell::const_new();
static KWIN_EXACT_CAPTURE: OnceCell<bool> = OnceCell::const_new();

/// Capture exact window-local pixels, returning `None` when the runtime lacks
/// compositor-native support. Once support is confirmed, capture failures are
/// returned rather than falling back to desktop pixels.
pub(crate) async fn capture_window_exact(
    window: &WindowInfo,
) -> Result<Option<RawScreenshotCapture>> {
    match window.backend.as_str() {
        KWIN_BACKEND => capture_kwin_window_exact(window).await,
        HYPRLAND_BACKEND => capture_hyprland_window_exact(window).await,
        _ => Ok(None),
    }
}

async fn capture_hyprland_window_exact(
    window: &WindowInfo,
) -> Result<Option<RawScreenshotCapture>> {
    if !grim_supports_exact_capture().await {
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

async fn capture_kwin_window_exact(window: &WindowInfo) -> Result<Option<RawScreenshotCapture>> {
    if !kwin_supports_exact_capture().await {
        return Ok(None);
    }
    install_kwin_capture_desktop_file()?;
    let uuid = kwin::kwin_uuid_for_window_id(window.window_id)
        .await?
        .with_context(|| {
            format!(
                "No KWin window matched window_id {} during exact capture",
                window.window_id
            )
        })?;
    let raw = capture_kwin_raw(&uuid).await?;
    let png = tokio::task::spawn_blocking(move || encode_kwin_raw_as_png(raw))
        .await
        .context("KWin exact screenshot encode task failed")??;
    Ok(Some(png))
}

async fn kwin_supports_exact_capture() -> bool {
    *KWIN_EXACT_CAPTURE
        .get_or_init(|| async {
            hydrate_session_bus_env();
            let Ok(connection) = Connection::session().await else {
                return false;
            };
            let Ok(proxy) = Proxy::new(
                &connection,
                KWIN_SCREENSHOT_SERVICE,
                KWIN_SCREENSHOT_PATH,
                "org.freedesktop.DBus.Introspectable",
            )
            .await
            else {
                return false;
            };
            tokio::time::timeout(
                EXACT_CAPTURE_PROBE_TIMEOUT,
                proxy.call::<_, _, String>("Introspect", &()),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .is_some_and(|xml| xml.contains("CaptureWindow"))
        })
        .await
}

#[derive(Debug)]
struct KwinRawCapture {
    bytes: Vec<u8>,
    width: u32,
    height: u32,
    stride: usize,
    format: u32,
}

async fn capture_kwin_raw(uuid: &str) -> Result<KwinRawCapture> {
    hydrate_session_bus_env();
    let connection = Connection::session()
        .await
        .context("failed to connect to the session bus for KWin exact capture")?;
    let proxy = Proxy::new(
        &connection,
        KWIN_SCREENSHOT_SERVICE,
        KWIN_SCREENSHOT_PATH,
        KWIN_SCREENSHOT_INTERFACE,
    )
    .await
    .context("failed to create KWin ScreenShot2 proxy")?;
    let (reader, writer) = UnixStream::pair().context("failed to create KWin capture pipe")?;
    reader
        .set_read_timeout(Some(EXACT_CAPTURE_TIMEOUT))
        .context("failed to configure KWin capture timeout")?;
    let reader_task = tokio::task::spawn_blocking(move || {
        let mut bytes = Vec::new();
        reader
            .take((KWIN_CAPTURE_MAX_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .context("failed to read KWin capture pixels")?;
        if bytes.len() > KWIN_CAPTURE_MAX_BYTES {
            bail!("KWin capture data exceeds the safety limit");
        }
        Ok::<_, anyhow::Error>(bytes)
    });
    let mut options: HashMap<&str, Value<'_>> = HashMap::new();
    options.insert("include-decoration", false.into());
    options.insert("include-shadow", false.into());
    options.insert("native-resolution", true.into());
    let descriptor = Fd::from(&writer);
    let reply: HashMap<String, OwnedValue> = tokio::time::timeout(
        EXACT_CAPTURE_TIMEOUT,
        proxy.call("CaptureWindow", &(uuid, options, descriptor)),
    )
    .await
    .context("KWin exact window capture timed out")?
    .context("KWin ScreenShot2 CaptureWindow failed")?;
    drop(writer);
    let bytes = reader_task
        .await
        .context("KWin capture pipe task failed")??;

    let capture_type = metadata_string(&reply, "type")?;
    if capture_type != "raw" {
        bail!("KWin returned unsupported capture type {capture_type:?}");
    }
    let width = metadata_u32(&reply, "width")?;
    let height = metadata_u32(&reply, "height")?;
    let stride = metadata_u32(&reply, "stride")? as usize;
    let format = metadata_u32(&reply, "format")?;
    validate_kwin_capture_metadata(width, height, stride, bytes.len())?;
    Ok(KwinRawCapture {
        bytes,
        width,
        height,
        stride,
        format,
    })
}

fn metadata_u32(metadata: &HashMap<String, OwnedValue>, key: &str) -> Result<u32> {
    let value = metadata
        .get(key)
        .with_context(|| format!("KWin capture metadata omitted {key}"))?;
    if let Ok(value) = u32::try_from(value) {
        return Ok(value);
    }
    let value = i32::try_from(value)
        .with_context(|| format!("KWin capture metadata {key} was not an integer"))?;
    u32::try_from(value).with_context(|| format!("KWin capture metadata {key} was negative"))
}

fn metadata_string(metadata: &HashMap<String, OwnedValue>, key: &str) -> Result<String> {
    let value = metadata
        .get(key)
        .with_context(|| format!("KWin capture metadata omitted {key}"))?;
    <&str>::try_from(value)
        .map(ToOwned::to_owned)
        .with_context(|| format!("KWin capture metadata {key} was not a string"))
}

fn validate_kwin_capture_metadata(
    width: u32,
    height: u32,
    stride: usize,
    byte_count: usize,
) -> Result<()> {
    if width == 0 || height == 0 || stride < width as usize * 4 {
        bail!("KWin returned invalid capture dimensions or stride");
    }
    let expected = stride
        .checked_mul(height as usize)
        .context("KWin capture dimensions overflowed")?;
    if expected > KWIN_CAPTURE_MAX_BYTES || byte_count != expected {
        bail!(
            "KWin returned incomplete or oversized capture data: expected {expected} bytes, got {byte_count}"
        );
    }
    Ok(())
}

fn encode_kwin_raw_as_png(raw: KwinRawCapture) -> Result<RawScreenshotCapture> {
    let mut image = RgbaImage::new(raw.width, raw.height);
    for y in 0..raw.height as usize {
        let row = &raw.bytes[y * raw.stride..y * raw.stride + raw.width as usize * 4];
        for (x, pixel) in row.chunks_exact(4).enumerate() {
            let rgba = kwin_pixel_to_rgba(raw.format, pixel)?;
            image.put_pixel(x as u32, y as u32, Rgba(rgba));
        }
    }
    let mut bytes = Vec::new();
    DynamicImage::ImageRgba8(image)
        .write_to(&mut Cursor::new(&mut bytes), ImageFormat::Png)
        .context("failed to encode KWin exact capture as PNG")?;
    Ok(RawScreenshotCapture {
        mime_type: "image/png".to_string(),
        bytes,
        source: "kwin-screenshot2-exact".to_string(),
        width: raw.width,
        height: raw.height,
    })
}

fn kwin_pixel_to_rgba(format: u32, pixel: &[u8]) -> Result<[u8; 4]> {
    let (red, green, blue, alpha, premultiplied) = match format {
        // QImage::Format_RGB32, ARGB32, and ARGB32_Premultiplied use native
        // 0xAARRGGBB words. KWin and Qt support little- and big-endian hosts.
        4..=6 if cfg!(target_endian = "little") => (
            pixel[2],
            pixel[1],
            pixel[0],
            if format == 4 { 255 } else { pixel[3] },
            format == 6,
        ),
        4..=6 => (
            pixel[1],
            pixel[2],
            pixel[3],
            if format == 4 { 255 } else { pixel[0] },
            format == 6,
        ),
        // QImage::Format_RGBX8888, RGBA8888, RGBA8888_Premultiplied.
        16..=18 => (
            pixel[0],
            pixel[1],
            pixel[2],
            if format == 16 { 255 } else { pixel[3] },
            format == 18,
        ),
        _ => bail!("KWin returned unsupported QImage format {format}"),
    };
    if !premultiplied || alpha == 0 || alpha == 255 {
        return Ok([red, green, blue, alpha]);
    }
    let unpremultiply = |channel: u8| {
        ((u32::from(channel) * 255 + u32::from(alpha) / 2) / u32::from(alpha)).min(255) as u8
    };
    Ok([
        unpremultiply(red),
        unpremultiply(green),
        unpremultiply(blue),
        alpha,
    ])
}

fn install_kwin_capture_desktop_file() -> Result<()> {
    let Some(data_home) = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share")))
    else {
        bail!("HOME and XDG_DATA_HOME are unset; cannot register KWin screenshot permission");
    };
    let executable =
        std::env::current_exe().context("failed to resolve the Computer Use executable")?;
    let desktop = data_home.join("applications/computer-use-linux.desktop");
    let escaped = executable
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('"', "\\\"");
    let contents = format!(
        "[Desktop Entry]\nType=Application\nName=Computer Use Linux\nNoDisplay=true\nExec=\"{escaped}\" %U\nX-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2\n"
    );
    if fs::read_to_string(&desktop).ok().as_deref() == Some(contents.as_str()) {
        return Ok(());
    }
    let parent = desktop
        .parent()
        .context("KWin desktop file has no parent")?;
    fs::create_dir_all(parent).context("failed to create user applications directory")?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let temporary = desktop.with_extension(format!("desktop.{}.{nonce}.tmp", std::process::id()));
    let temporary_guard = TemporaryCapture::new(temporary.clone());
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o644)
        .open(&temporary)
        .context("failed to create KWin desktop permission file")?
        .write_all(contents.as_bytes())
        .context("failed to write KWin desktop permission file")?;
    fs::rename(&temporary, &desktop).context("failed to install KWin desktop permission file")?;
    drop(temporary_guard);
    if let Some(updater) = ["kbuildsycoca6", "kbuildsycoca5"]
        .into_iter()
        .find(|candidate| command_exists(candidate))
    {
        let _ = std::process::Command::new(updater)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
    Ok(())
}

fn command_exists(command: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|directory| directory.join(command).is_file())
    })
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
