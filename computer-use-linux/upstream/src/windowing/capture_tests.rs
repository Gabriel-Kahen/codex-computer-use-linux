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
