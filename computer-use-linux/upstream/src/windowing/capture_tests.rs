use super::*;

#[test]
fn detects_exact_capture_flag_in_grim_help_streams() {
    assert!(grim_help_supports_exact(
        b"Usage: grim\n  -T <identifier> capture a toplevel",
        b""
    ));
    assert!(grim_help_supports_exact(b"", b"grim: -T <identifier>"));
    assert!(!grim_help_supports_exact(
        b"Usage: grim\n  -g <geometry>",
        b""
    ));
}

#[test]
fn temporary_exact_capture_is_removed_on_drop() {
    let path = temp_png_path("exact-cleanup-test");
    fs::write(&path, b"partial png").unwrap();
    {
        let temporary = TemporaryCapture::new(path.clone());
        assert_eq!(temporary.path(), path);
    }
    assert!(!path.exists());
}

#[test]
fn converts_qimage_native_and_rgba_pixels() {
    let native = if cfg!(target_endian = "little") {
        [10, 20, 30, 255]
    } else {
        [255, 30, 20, 10]
    };
    assert_eq!(kwin_pixel_to_rgba(5, &native).unwrap(), [30, 20, 10, 255]);
    assert_eq!(
        kwin_pixel_to_rgba(17, &[30, 20, 10, 128]).unwrap(),
        [30, 20, 10, 128]
    );
}

#[test]
fn unpremultiplies_qimage_alpha() {
    assert_eq!(
        kwin_pixel_to_rgba(18, &[64, 32, 16, 128]).unwrap(),
        [128, 64, 32, 128]
    );
}

#[test]
fn validates_exact_raw_capture_size() {
    assert!(validate_kwin_capture_metadata(10, 4, 48, 192).is_ok());
    assert!(validate_kwin_capture_metadata(10, 4, 39, 156).is_err());
    assert!(validate_kwin_capture_metadata(10, 4, 48, 191).is_err());
}

#[test]
fn accepts_qt_signed_capture_metadata() {
    let metadata = HashMap::from([
        ("width".to_string(), OwnedValue::from(1280_i32)),
        ("height".to_string(), OwnedValue::from(720_u32)),
    ]);
    assert_eq!(metadata_u32(&metadata, "width").unwrap(), 1280);
    assert_eq!(metadata_u32(&metadata, "height").unwrap(), 720);
}
