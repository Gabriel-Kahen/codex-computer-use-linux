use anyhow::{bail, Context, Result};
use image::ImageEncoder;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use wayland_client::protocol::wl_shm;

pub(super) struct BackingFile {
    path: PathBuf,
    pub(super) file: File,
}

impl BackingFile {
    pub(super) fn new(byte_len: u64) -> Result<Self> {
        static NEXT_FILE: AtomicU64 = AtomicU64::new(1);
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        for _ in 0..16 {
            let sequence = NEXT_FILE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "computer-use-linux-cosmic-shm-{}-{timestamp}-{sequence}",
                std::process::id()
            ));
            match OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
            {
                Ok(file) => {
                    if let Err(error) = file.set_len(byte_len) {
                        let _ = fs::remove_file(&path);
                        return Err(error).context("failed to size COSMIC capture SHM file");
                    }
                    return Ok(Self { path, file });
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    return Err(error).context("failed to create COSMIC capture SHM file");
                }
            }
        }
        bail!("failed to allocate a unique COSMIC capture SHM file")
    }

    pub(super) fn read_all(&mut self, byte_len: usize) -> Result<Vec<u8>> {
        self.file
            .seek(SeekFrom::Start(0))
            .context("failed to rewind COSMIC capture SHM file")?;
        let mut pixels = vec![0; byte_len];
        self.file
            .read_exact(&mut pixels)
            .context("failed to read COSMIC capture pixels")?;
        Ok(pixels)
    }
}

impl Drop for BackingFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub(super) fn write_png(
    output_path: &Path,
    width: u32,
    height: u32,
    format: wl_shm::Format,
    mut pixels: Vec<u8>,
) -> Result<()> {
    for pixel in pixels.chunks_exact_mut(4) {
        let packed = u32::from_le_bytes(pixel.try_into().expect("four-byte pixel chunk"));
        let alpha = if format == wl_shm::Format::Abgr8888 {
            (packed >> 24) as u8
        } else {
            u8::MAX
        };
        pixel.copy_from_slice(&[
            unpremultiply(packed as u8, alpha),
            unpremultiply((packed >> 8) as u8, alpha),
            unpremultiply((packed >> 16) as u8, alpha),
            alpha,
        ]);
    }
    let mut staged = StagedOutput::new(output_path)?;
    image::codecs::png::PngEncoder::new(&mut staged.file)
        .write_image(&pixels, width, height, image::ExtendedColorType::Rgba8)
        .context("failed to encode COSMIC capture PNG")?;
    staged
        .file
        .flush()
        .context("failed to flush private COSMIC capture output")?;
    staged
        .file
        .sync_all()
        .context("failed to sync private COSMIC capture output")?;
    staged.publish(output_path)
}

fn unpremultiply(channel: u8, alpha: u8) -> u8 {
    match alpha {
        0 => 0,
        u8::MAX => channel,
        _ => ((u32::from(channel) * 255 + u32::from(alpha) / 2) / u32::from(alpha)).min(255) as u8,
    }
}

struct StagedOutput {
    path: PathBuf,
    file: File,
}

impl StagedOutput {
    fn new(output_path: &Path) -> Result<Self> {
        static NEXT_OUTPUT: AtomicU64 = AtomicU64::new(1);
        let parent = output_path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        for _ in 0..16 {
            let sequence = NEXT_OUTPUT.fetch_add(1, Ordering::Relaxed);
            let path = parent.join(format!(
                ".computer-use-linux-cosmic-output-{}-{timestamp}-{sequence}.tmp",
                std::process::id()
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
            {
                Ok(file) => return Ok(Self { path, file }),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    return Err(error).with_context(|| {
                        format!(
                            "failed to create private COSMIC capture beside {}",
                            output_path.display()
                        )
                    });
                }
            }
        }
        bail!("failed to allocate a unique private COSMIC capture output")
    }

    fn publish(&self, output_path: &Path) -> Result<()> {
        fs::hard_link(&self.path, output_path).with_context(|| {
            format!(
                "failed to publish COSMIC capture to {} without overwriting an existing path",
                output_path.display()
            )
        })
    }
}

impl Drop for StagedOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}
