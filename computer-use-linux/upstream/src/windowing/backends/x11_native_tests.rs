use super::*;
use std::io;

#[test]
fn parses_icccm_wm_class_fields() {
    assert_eq!(
        parse_wm_class(b"Navigator\0firefox\0"),
        (Some("Navigator".to_string()), Some("firefox".to_string()))
    );
    assert_eq!(
        parse_wm_class(b"terminal\0"),
        (Some("terminal".to_string()), Some("terminal".to_string()))
    );
}

#[test]
fn cleans_bounded_x11_text_values() {
    assert_eq!(
        clean_bytes(b"  Firefox \0".to_vec()).as_deref(),
        Some("Firefox")
    );
    assert_eq!(clean_bytes(b"\0".to_vec()), None);
}

#[test]
fn snapshot_cache_expires_and_invalidates_window_metadata() {
    let captured_at = Instant::now();
    let window = WindowInfo {
        window_id: 42,
        title: Some("first title".to_string()),
        app_id: None,
        wm_class: None,
        pid: None,
        bounds: None,
        workspace: None,
        focused: false,
        hidden: false,
        client_type: Some("x11".to_string()),
        backend: X11_BACKEND.to_string(),
        terminal: None,
    };
    let mut cache = SnapshotCache::default();

    cache.replace(captured_at, vec![window]);
    assert_eq!(
        cache
            .fresh_windows(captured_at)
            .and_then(|windows| windows.first().map(|window| window.window_id)),
        Some(42)
    );
    assert_eq!(
        cache.window(42).map(|window| window.title.as_deref()),
        Some(Some("first title"))
    );

    cache.invalidate();
    assert!(cache.fresh_windows(captured_at).is_none());
    assert_eq!(cache.window(42).map(|window| window.window_id), None);

    cache.replace(captured_at, Vec::new());
    assert!(cache.fresh_windows(captured_at + SNAPSHOT_TTL).is_none());
}

#[test]
fn only_transport_failures_discard_the_persistent_session() {
    let direct = anyhow::Error::new(ConnectionError::IoError(io::Error::other("closed")));
    let reply = anyhow::Error::new(ReplyError::ConnectionError(ConnectionError::IoError(
        io::Error::other("closed"),
    )));

    assert!(is_connection_failure(&direct));
    assert!(is_connection_failure(&reply));
    assert!(!is_connection_failure(&anyhow::anyhow!(
        "window is not managed"
    )));
}

#[test]
fn rejects_window_ids_outside_the_x11_protocol_range_without_connecting() {
    assert_eq!(
        activate_window(u64::from(u32::MAX) + 1).unwrap(),
        NativeActivation::WindowNotManaged
    );
}
