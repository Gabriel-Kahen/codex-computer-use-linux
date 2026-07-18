use super::*;
use crate::accessibility_snapshot::AccessibilitySnapshotTarget;
use crate::atspi_tree::AccessibilityNode;
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
        observed_element: None,
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
        observed_element: None,
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
            observed_element: None,
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

fn observed_node() -> AccessibilityNode {
    AccessibilityNode {
        index: 42,
        parent_index: None,
        depth: 0,
        object_ref: ":1.42/org/a11y/atspi/accessible/42".to_string(),
        role: "push button".to_string(),
        name: Some("Button 42".to_string()),
        description: None,
        child_count: 0,
        bounds: Some(Bounds {
            x: 20,
            y: 30,
            width: 100,
            height: 100,
        }),
        states: Vec::new(),
        actions: Vec::new(),
        value: None,
        text: None,
        supports_editable_text: false,
    }
}

fn record_snapshot(
    snapshots: &Mutex<AccessibilitySnapshotStore>,
    target: AccessibilitySnapshotTarget,
    node: &AccessibilityNode,
) -> String {
    snapshots
        .lock()
        .unwrap()
        .record(target, std::slice::from_ref(node))
}

fn verify_snapshot_state(
    snapshots: &Mutex<AccessibilitySnapshotStore>,
    verification: &PointerDispatchVerification,
    focused: &WindowInfo,
) -> Result<(), String> {
    verify_pointer_dispatch_snapshot_state(verification, Some(focused), |observation_id| {
        resolve_snapshot(snapshots, observation_id)
    })
}

#[tokio::test]
async fn observed_pointer_verification_rejects_stale_or_changed_targets() {
    let snapshots = Mutex::new(AccessibilitySnapshotStore::default());
    let target = AccessibilitySnapshotTarget::Window {
        window_id: 42,
        pid: Some(4242),
    };
    let node = observed_node();
    let mut verification = PointerDispatchVerification {
        exact_window_id: 42,
        expected_pid: Some(4242),
        observed_element: Some(ObservedElementPointer {
            observation_id: record_snapshot(&snapshots, target.clone(), &node),
            object_ref: node.object_ref.clone(),
            point: (70, 80),
        }),
    };
    let mut focused = window_info(42, Some(4242));
    verify_snapshot_state(&snapshots, &verification, &focused).unwrap();

    verification.observed_element.as_mut().unwrap().object_ref = ":1.42/forged".to_string();
    assert!(verify_snapshot_state(&snapshots, &verification, &focused)
        .unwrap_err()
        .contains("no longer belongs"));
    let observed = verification.observed_element.as_mut().unwrap();
    observed.object_ref = node.object_ref.clone();
    observed.point = (69, 80);
    assert!(verify_snapshot_state(&snapshots, &verification, &focused)
        .unwrap_err()
        .contains("does not match"));
    verification.observed_element.as_mut().unwrap().point = (70, 80);

    focused.bounds = Some(WindowBounds {
        x: Some(200),
        y: Some(200),
        width: 100,
        height: 100,
    });
    assert!(verify_snapshot_state(&snapshots, &verification, &focused).is_err());
    focused = window_info(42, Some(4242));
    record_snapshot(&snapshots, target.clone(), &node);
    assert!(verify_snapshot_state(&snapshots, &verification, &focused)
        .unwrap_err()
        .contains("stale"));
    let moved_bounds = Bounds {
        x: 100,
        y: 100,
        width: 20,
        height: 20,
    };
    assert!(ensure_element_point_in_live_bounds((70, 80), &moved_bounds).is_err());

    for snapshot_target in [
        AccessibilitySnapshotTarget::Window {
            window_id: 43,
            pid: Some(4242),
        },
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: None,
        },
        AccessibilitySnapshotTarget::Desktop,
    ] {
        verification
            .observed_element
            .as_mut()
            .unwrap()
            .observation_id = record_snapshot(&snapshots, snapshot_target, &node);
        assert!(verify_snapshot_state(&snapshots, &verification, &focused).is_err());
    }

    verification
        .observed_element
        .as_mut()
        .unwrap()
        .observation_id = record_snapshot(&snapshots, target, &node);
    let focused_for_probe = focused.clone();
    verify_pointer_dispatch_with(
        Some(&verification),
        |observation_id| resolve_snapshot(&snapshots, observation_id),
        |_| async {
            Ok::<_, anyhow::Error>(Bounds {
                x: 50,
                y: 60,
                width: 40,
                height: 40,
            })
        },
        move || async move { Ok::<_, anyhow::Error>(Some(focused_for_probe)) },
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn failed_verification_blocks_every_pointer_dispatch_boundary() {
    for boundary in [
        PointerDispatchBoundary::AbsolutePointer,
        PointerDispatchBoundary::CachedPortal,
        PointerDispatchBoundary::NewPortal,
        PointerDispatchBoundary::Ydotool,
    ] {
        let dispatched = std::cell::Cell::new(false);
        let result: Result<(), String> =
            run_verified_pointer_dispatch(boundary, async { Err("blocked".to_string()) }, async {
                dispatched.set(true)
            })
            .await;

        assert_eq!(result, Err("blocked".to_string()));
        assert!(!dispatched.get(), "dispatched through {boundary:?}");
    }
}
