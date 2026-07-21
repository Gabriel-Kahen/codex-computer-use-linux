use super::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn accepts_cosmic_eight_bit_shm_formats_in_preference_order() {
    assert_eq!(
        select_shm_format(&[
            WEnum::Value(wl_shm::Format::Xbgr8888),
            WEnum::Value(wl_shm::Format::Abgr8888),
        ])
        .unwrap(),
        wl_shm::Format::Abgr8888
    );
}

#[test]
fn rejects_unsupported_shm_formats() {
    let error = select_shm_format(&[WEnum::Value(wl_shm::Format::Abgr2101010)])
        .unwrap_err()
        .to_string();

    assert!(error.contains("supported 8-bit RGBA"));
}

#[test]
fn capture_layout_is_bounded() {
    let layout = BufferLayout::new(640, 480).unwrap();
    assert_eq!(
        (
            layout.width_i32,
            layout.height_i32,
            layout.stride_i32,
            layout.byte_len_i32,
        ),
        (640, 480, 2_560, 1_228_800)
    );
    assert!(BufferLayout::new(16_384, 16_384).is_err());
}

#[test]
fn xbgr_png_forces_opaque_alpha() {
    let path = std::env::temp_dir().join(format!(
        "computer-use-linux-cosmic-pixel-test-{}-{}.png",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    write_png(&path, 1, 1, wl_shm::Format::Xbgr8888, vec![12, 34, 56, 0]).unwrap();
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let decoded = image::open(&path).unwrap().into_rgba8();
    fs::remove_file(&path).unwrap();

    assert_eq!(decoded.into_raw(), vec![12, 34, 56, 255]);
}

#[test]
fn abgr_png_preserves_alpha() {
    let path = std::env::temp_dir().join(format!(
        "computer-use-linux-cosmic-alpha-test-{}-{}.png",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    write_png(&path, 1, 1, wl_shm::Format::Abgr8888, vec![12, 34, 56, 78]).unwrap();
    let decoded = image::open(&path).unwrap().into_rgba8();
    fs::remove_file(&path).unwrap();

    assert_eq!(decoded.into_raw(), vec![39, 111, 183, 78]);
}

#[test]
fn png_publish_preserves_an_existing_output() {
    let path = std::env::temp_dir().join(format!(
        "computer-use-linux-cosmic-existing-test-{}-{}.png",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::write(&path, b"existing").unwrap();

    let error = write_png(&path, 1, 1, wl_shm::Format::Xbgr8888, vec![12, 34, 56, 0])
        .unwrap_err()
        .to_string();

    assert!(error.contains("without overwriting"));
    assert_eq!(fs::read(&path).unwrap(), b"existing");
    fs::remove_file(path).unwrap();
}

#[test]
fn png_publish_does_not_follow_an_existing_symlink() {
    let suffix = format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let target = std::env::temp_dir().join(format!("cosmic-target-{suffix}"));
    let output = std::env::temp_dir().join(format!("cosmic-link-{suffix}.png"));
    fs::write(&target, b"target").unwrap();
    std::os::unix::fs::symlink(&target, &output).unwrap();

    assert!(write_png(&output, 1, 1, wl_shm::Format::Xbgr8888, vec![12, 34, 56, 0],).is_err());
    assert_eq!(fs::read(&target).unwrap(), b"target");
    assert!(fs::symlink_metadata(&output)
        .unwrap()
        .file_type()
        .is_symlink());
    fs::remove_file(output).unwrap();
    fs::remove_file(target).unwrap();
}
