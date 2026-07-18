use super::*;
use crate::accessibility_snapshot::AccessibilitySnapshotTarget;
use crate::atspi_tree::{AccessibilityAction, AccessibilityNode, Bounds};

fn node(bounds: Option<Bounds>) -> AccessibilityNode {
    AccessibilityNode {
        index: 7,
        parent_index: None,
        depth: 0,
        object_ref: ":1.7/org/a11y/atspi/accessible/7".to_string(),
        role: "push button".to_string(),
        name: Some("Run".to_string()),
        description: None,
        child_count: 0,
        bounds,
        states: Vec::new(),
        actions: vec![AccessibilityAction {
            index: 0,
            name: "Click".to_string(),
            description: String::new(),
            keybinding: String::new(),
        }],
        value: None,
        text: None,
        supports_editable_text: false,
    }
}

fn record(
    backend: &ComputerUseLinux,
    target: AccessibilitySnapshotTarget,
    node: AccessibilityNode,
) -> String {
    backend.record_accessibility_snapshot(target, &[node])
}

fn params(observation_id: Option<String>) -> ClickParams {
    ClickParams {
        observation_id,
        element_index: Some(7),
        ..Default::default()
    }
}

#[test]
fn observed_clicks_reject_ambiguous_or_unbound_input() {
    let backend = ComputerUseLinux::default();
    for (params, message) in [
        (
            ClickParams {
                role: Some("button".to_string()),
                x: Some(10),
                y: Some(20),
                ..Default::default()
            },
            "Do not combine click coordinates",
        ),
        (
            ClickParams {
                x: Some(10),
                ..Default::default()
            },
            "require both x and y",
        ),
        (
            ClickParams {
                element_index: Some(7),
                relative: Some(true),
                ..Default::default()
            },
            "relative=true is not supported",
        ),
        (params(None), "observation_id is required"),
    ] {
        assert!(backend
            .resolve_observed_click_target(&params)
            .unwrap_err()
            .contains(message));
    }
    assert!(matches!(
        backend
            .resolve_observed_click_target(&ClickParams {
                x: Some(10),
                y: Some(20),
                ..Default::default()
            })
            .unwrap(),
        ClickTarget::Coordinates(10, 20)
    ));
}

#[test]
fn observed_clicks_require_exact_window_scope_and_pid() {
    let backend = ComputerUseLinux::default();
    let bounds = Some(Bounds {
        x: 10,
        y: 20,
        width: 100,
        height: 40,
    });
    let desktop_id = record(
        &backend,
        AccessibilitySnapshotTarget::Desktop,
        node(bounds.clone()),
    );
    assert!(backend
        .resolve_observed_click_target(&params(Some(desktop_id)))
        .unwrap_err()
        .contains("window-scoped observation"));

    let observation_id = record(
        &backend,
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        node(bounds),
    );
    let ClickTarget::ObservedCoordinates(target) = backend
        .resolve_observed_click_target(&params(Some(observation_id.clone())))
        .unwrap()
    else {
        panic!("expected observed coordinates");
    };
    assert_eq!(
        (target.point, target.window_id, target.pid),
        ((60, 40), 42, Some(4242))
    );
    assert!(backend
        .resolve_observed_click_target(&ClickParams {
            pid: Some(4343),
            ..params(Some(observation_id))
        })
        .unwrap_err()
        .contains("does not match"));
}

#[test]
fn observed_clicks_fail_closed_without_usable_bounds() {
    let backend = ComputerUseLinux::default();
    for bounds in [
        None,
        Some(Bounds {
            x: i32::MIN,
            y: i32::MIN,
            width: 1,
            height: 1,
        }),
    ] {
        let observation_id = record(
            &backend,
            AccessibilitySnapshotTarget::Window {
                window_id: 42,
                pid: Some(4242),
            },
            node(bounds),
        );
        let error = backend
            .resolve_observed_click_target(&ClickParams {
                observation_id: Some(observation_id),
                role: Some("button".to_string()),
                name: Some("run".to_string()),
                ..Default::default()
            })
            .unwrap_err();
        assert!(error.contains("perform_action with the same observation_id"));
    }
}
