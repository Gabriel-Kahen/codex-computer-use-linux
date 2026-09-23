use super::*;
use crate::accessibility_snapshot::AccessibilitySnapshotTarget;
use crate::atspi_tree::{AccessibilityNode, Bounds};
use crate::windows::{WindowBounds, WindowInfo, GNOME_SHELL_EXTENSION_BACKEND};

fn node(index: u32, bounds: Option<Bounds>) -> AccessibilityNode {
    AccessibilityNode {
        index,
        parent_index: None,
        depth: 0,
        object_ref: format!(":1.7/org/a11y/atspi/accessible/{index}"),
        role: "panel".to_string(),
        name: Some("Scrollable".to_string()),
        description: None,
        child_count: 0,
        bounds,
        states: Vec::new(),
        actions: Vec::new(),
        value: None,
        text: None,
        supports_editable_text: false,
    }
}

fn request<'a>(observation_id: Option<&'a str>) -> ScrollTargetRequest<'a> {
    ScrollTargetRequest {
        observation_id,
        element_index: None,
        x: None,
        y: None,
        relative: false,
        window_target: None,
    }
}

fn window(window_id: u64, pid: Option<u32>, bounds: Option<WindowBounds>) -> WindowInfo {
    WindowInfo {
        window_id,
        title: Some("Target".to_string()),
        app_id: Some("target-app".to_string()),
        wm_class: None,
        pid,
        bounds,
        workspace: Some(0),
        focused: true,
        hidden: false,
        client_type: Some("wayland".to_string()),
        backend: GNOME_SHELL_EXTENSION_BACKEND.to_string(),
        terminal: None,
    }
}

fn error_for(request: ScrollTargetRequest<'_>) -> String {
    match resolve_observed_scroll_target(
        &Mutex::new(AccessibilitySnapshotStore::default()),
        request,
    ) {
        Ok(_) => panic!("expected target validation to fail"),
        Err(error) => error,
    }
}

#[test]
fn target_shape_rejects_ambiguous_element_inputs() {
    let mut partial_x = request(None);
    partial_x.x = Some(1);
    assert!(error_for(partial_x).contains("both x and y"));

    let mut mixed = request(Some("obs"));
    mixed.element_index = Some(7);
    mixed.x = Some(1);
    mixed.y = Some(2);
    assert!(error_for(mixed).contains("Do not combine"));

    let mut missing_id = request(None);
    missing_id.element_index = Some(7);
    assert!(error_for(missing_id).contains("observation_id is required"));

    let mut relative = request(Some("obs"));
    relative.element_index = Some(7);
    relative.relative = true;
    assert!(error_for(relative).contains("relative=true"));

    assert!(error_for(request(Some("obs"))).contains("only valid with element_index"));
}

#[test]
fn element_target_comes_only_from_the_window_observation() {
    let snapshots = Mutex::new(AccessibilitySnapshotStore::default());
    let observed_node = node(
        7,
        Some(Bounds {
            x: 10,
            y: 20,
            width: 100,
            height: 40,
        }),
    );
    let observation_id = snapshots.lock().unwrap().record(
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        std::slice::from_ref(&observed_node),
    );
    let mut input = request(Some(&observation_id));
    input.element_index = Some(7);
    let target = resolve_observed_scroll_target(&snapshots, input)
        .unwrap()
        .unwrap();

    assert_eq!(target.window_target.window_id, Some(42));
    let (prepared_window, verification) = target
        .prepare_for_window(&window(
            42,
            Some(4242),
            Some(WindowBounds {
                x: Some(0),
                y: Some(0),
                width: 800,
                height: 600,
            }),
        ))
        .unwrap();
    assert_eq!(prepared_window.window_id, Some(42));
    assert_eq!(
        verification,
        PointerDispatchVerification {
            exact_window_id: 42,
            expected_pid: Some(4242),
            observed_element: Some(ObservedElementPointer {
                observation_id,
                object_ref: observed_node.object_ref,
                point: (60, 40),
            }),
        }
    );
}

#[test]
fn element_target_rejects_wrong_scope_identity_and_bounds() {
    let snapshots = Mutex::new(AccessibilitySnapshotStore::default());
    let valid_node = node(
        7,
        Some(Bounds {
            x: 10,
            y: 20,
            width: 100,
            height: 40,
        }),
    );
    let desktop_id = snapshots.lock().unwrap().record(
        AccessibilitySnapshotTarget::Desktop,
        std::slice::from_ref(&valid_node),
    );
    let mut input = request(Some(&desktop_id));
    input.element_index = Some(7);
    assert!(resolve_observed_scroll_target(&snapshots, input)
        .unwrap_err()
        .contains("window-scoped"));

    let invalid_node = node(7, None);
    let observation_id = snapshots.lock().unwrap().record(
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: None,
        },
        &[invalid_node],
    );
    let mut input = request(Some(&observation_id));
    input.element_index = Some(7);
    assert!(resolve_observed_scroll_target(&snapshots, input)
        .unwrap_err()
        .contains("No scrollable bounds"));

    let observation_id = snapshots.lock().unwrap().record(
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: None,
        },
        &[valid_node],
    );
    for window_target in [
        WindowTarget {
            window_id: Some(43),
            ..Default::default()
        },
        WindowTarget {
            pid: Some(7),
            ..Default::default()
        },
    ] {
        let mut input = request(Some(&observation_id));
        input.element_index = Some(7);
        input.window_target = Some(window_target);
        assert!(resolve_observed_scroll_target(&snapshots, input)
            .unwrap_err()
            .contains("does not match the requested target window"));
    }
    let mut input = request(Some(&observation_id));
    input.element_index = Some(7);
    input.window_target = Some(WindowTarget {
        app_id: Some("other-app".to_string()),
        ..Default::default()
    });
    let target = resolve_observed_scroll_target(&snapshots, input)
        .unwrap()
        .unwrap();
    assert_eq!(target.window_target.window_id, None);
    assert_eq!(target.window_target.app_id.as_deref(), Some("other-app"));
    let mut other_app = window(43, Some(7), None);
    other_app.app_id = Some("other-app".to_string());
    assert!(target
        .prepare_from_windows(&[other_app])
        .unwrap_err()
        .contains("does not match target window_id"));
    assert!(target
        .prepare_for_window(&window(42, Some(7), None))
        .is_err());
}
