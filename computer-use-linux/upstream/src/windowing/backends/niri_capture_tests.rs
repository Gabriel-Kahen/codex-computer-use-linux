use super::*;

fn window(window_id: u64) -> WindowInfo {
    WindowInfo {
        window_id,
        title: Some("Editor".to_string()),
        app_id: Some("org.example.Editor".to_string()),
        wm_class: Some("org.example.Editor".to_string()),
        pid: Some(42),
        bounds: None,
        workspace: Some(3),
        focused: false,
        hidden: false,
        client_type: None,
        backend: NIRI_BACKEND.to_string(),
        terminal: None,
    }
}

#[test]
fn pipeline_targets_only_the_published_pipewire_node() {
    let path = Path::new("/tmp/niri exact.png");

    assert_eq!(
        gstreamer_pipeline_args(73, path),
        [
            "-q",
            "-e",
            "pipewiresrc",
            "path=73",
            "do-timestamp=true",
            "num-buffers=1",
            "!",
            "videoconvert",
            "!",
            "pngenc",
            "snapshot=true",
            "!",
            "filesink",
            "location=/tmp/niri exact.png",
        ]
        .map(OsString::from)
    );
}

#[test]
fn requires_the_niri_window_capture_protocol_version() {
    validate_screencast_version(MIN_SCREENCAST_VERSION).unwrap();
    validate_screencast_version(MIN_SCREENCAST_VERSION + 1).unwrap();

    assert!(validate_screencast_version(MIN_SCREENCAST_VERSION - 1)
        .unwrap_err()
        .to_string()
        .contains("too old"));
}

#[test]
fn stable_target_validation_accepts_metadata_preserving_changes() {
    let expected = window(8);
    let mut current = expected.clone();
    current.title = Some("Editor — second file".to_string());
    current.workspace = Some(4);

    assert_eq!(
        validate_current_window(&expected, Some(&current)).unwrap(),
        8
    );
}

#[test]
fn stable_target_validation_rejects_stale_and_reidentified_windows() {
    let expected = window(8);
    let missing = validate_current_window(&expected, None)
        .unwrap_err()
        .to_string();
    let mut changed = expected.clone();
    changed.pid = Some(99);
    let reidentified = validate_current_window(&expected, Some(&changed))
        .unwrap_err()
        .to_string();
    let mut changed_app = expected.clone();
    changed_app.app_id = Some("org.example.Other".to_string());
    let changed_application = validate_current_window(&expected, Some(&changed_app))
        .unwrap_err()
        .to_string();

    assert!(missing.contains("stale or closed"));
    assert!(reidentified.contains("changed process identity"));
    assert!(changed_application.contains("changed application identity"));
}

#[test]
fn stable_target_validation_rejects_hidden_windows() {
    let expected = window(8);
    let mut hidden = expected.clone();
    hidden.hidden = true;

    assert!(validate_current_window(&expected, Some(&hidden))
        .unwrap_err()
        .to_string()
        .contains("minimized or hidden"));
}
