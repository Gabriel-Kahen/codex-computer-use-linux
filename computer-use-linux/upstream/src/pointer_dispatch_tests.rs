use super::*;
use crate::windows::{WindowBounds, GNOME_SHELL_EXTENSION_BACKEND};

fn window_info(window_id: u64, pid: Option<u32>) -> WindowInfo {
    WindowInfo {
        window_id,
        title: Some("Target".to_string()),
        app_id: Some("target-app".to_string()),
        wm_class: None,
        pid,
        bounds: Some(WindowBounds {
            x: Some(10),
            y: Some(20),
            width: 800,
            height: 600,
        }),
        workspace: Some(0),
        focused: true,
        hidden: false,
        client_type: Some("wayland".to_string()),
        backend: GNOME_SHELL_EXTENSION_BACKEND.to_string(),
        terminal: None,
    }
}

#[test]
fn dispatch_requires_fresh_window_and_pid_identity() {
    let verification = PointerDispatchVerification {
        exact_window_id: 42,
        expected_pid: Some(4242),
    };
    let focused = window_info(42, Some(4242));
    verify_pointer_dispatch_state(&verification, Some(&focused)).unwrap();

    for mismatch in [window_info(41, Some(4242)), window_info(42, Some(4343))] {
        assert!(verify_pointer_dispatch_state(&verification, Some(&mismatch)).is_err());
    }
    assert!(verify_pointer_dispatch_state(&verification, None).is_err());

    let unknown_pid = PointerDispatchVerification {
        exact_window_id: 42,
        expected_pid: None,
    };
    assert!(verify_pointer_dispatch_state(&unknown_pid, Some(&focused)).is_err());
}

#[test]
fn verification_is_scoped_to_exact_absolute_targets() {
    let requested_window = window_info(42, Some(4242));
    let focus = WindowFocusResult {
        requested_window: requested_window.clone(),
        focused_window: Some(requested_window),
        exact_window_focused: true,
        app_focused: true,
        backend: GNOME_SHELL_EXTENSION_BACKEND.to_string(),
        note: "test focus".to_string(),
    };
    let exact_target = WindowTarget {
        window_id: Some(42),
        ..Default::default()
    };
    assert_eq!(
        pointer_dispatch_verification(&exact_target, None, Some(&focus)),
        Some(PointerDispatchVerification {
            exact_window_id: 42,
            expected_pid: Some(4242),
        })
    );
    assert_eq!(
        pointer_dispatch_verification(&exact_target, Some(true), Some(&focus)),
        None
    );
    assert_eq!(
        pointer_dispatch_verification(
            &WindowTarget {
                app_id: Some("target-app".to_string()),
                ..Default::default()
            },
            None,
            Some(&focus),
        ),
        None
    );
}
