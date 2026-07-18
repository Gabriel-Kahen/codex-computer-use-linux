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
                element_index: Some(999),
                role: Some("stale element metadata".to_string()),
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
        .resolve_observed_click_target(&ClickParams {
            window_id: Some(42),
            ..params(Some(observation_id.clone()))
        })
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
fn observed_plain_click_uses_stable_explicit_click_action() {
    let backend = ComputerUseLinux::default();
    let mut observed_node = node(Some(Bounds {
        x: 10,
        y: 20,
        width: 100,
        height: 40,
    }));
    observed_node.actions[0].index = 7;
    observed_node.actions[0].name = "cLiCk".to_string();
    observed_node.actions[0].description = "Press the button".to_string();
    let observation_id = record(
        &backend,
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        observed_node,
    );

    let ClickTarget::ObservedAction(target) = backend
        .resolve_observed_click_target(&params(Some(observation_id.clone())))
        .unwrap()
    else {
        panic!("expected an observation-bound AT-SPI action");
    };
    assert_eq!(target.observation_id, observation_id);
    assert_eq!(target.object_ref, ":1.7/org/a11y/atspi/accessible/7");
    assert_eq!(
        target.action_identity,
        ActionFingerprint::new("cLiCk", "Press the button").unwrap()
    );
}

#[test]
fn bounds_free_named_click_uses_stable_observation_bound_action() {
    let backend = ComputerUseLinux::default();
    let observation_id = record(
        &backend,
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        node(None),
    );

    assert!(matches!(
        backend
            .resolve_observed_click_target(&params(Some(observation_id)))
            .unwrap(),
        ClickTarget::ObservedAction(_)
    ));
}

#[test]
fn targeted_non_primary_and_non_click_actions_use_observed_coordinates() {
    let backend = ComputerUseLinux::default();
    let bounds = Some(Bounds {
        x: 10,
        y: 20,
        width: 100,
        height: 40,
    });
    let observation_id = record(
        &backend,
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        node(bounds.clone()),
    );
    for input in [
        ClickParams {
            window_id: Some(42),
            ..params(Some(observation_id.clone()))
        },
        ClickParams {
            button: Some("right".to_string()),
            ..params(Some(observation_id.clone()))
        },
        ClickParams {
            click_count: Some(2),
            ..params(Some(observation_id.clone()))
        },
    ] {
        assert!(matches!(
            backend.resolve_observed_click_target(&input).unwrap(),
            ClickTarget::ObservedCoordinates(_)
        ));
    }

    let mut activate = node(bounds);
    activate.actions[0].name = "activate".to_string();
    let observation_id = record(
        &backend,
        AccessibilitySnapshotTarget::Window {
            window_id: 42,
            pid: Some(4242),
        },
        activate,
    );
    assert!(matches!(
        backend
            .resolve_observed_click_target(&params(Some(observation_id)))
            .unwrap(),
        ClickTarget::ObservedCoordinates(_)
    ));
}

#[test]
fn invalidated_observed_click_action_is_rejected_before_dispatch() {
    let backend = ComputerUseLinux::default();
    let target = AccessibilitySnapshotTarget::Window {
        window_id: 42,
        pid: Some(4242),
    };
    let observation_id = record(
        &backend,
        target.clone(),
        node(Some(Bounds {
            x: 10,
            y: 20,
            width: 100,
            height: 40,
        })),
    );
    let ClickTarget::ObservedAction(action) = backend
        .resolve_observed_click_target(&params(Some(observation_id)))
        .unwrap()
    else {
        panic!("expected an observation-bound AT-SPI action");
    };
    backend
        .verify_observed_click_action_freshness(&action)
        .unwrap();

    record(
        &backend,
        target,
        node(Some(Bounds {
            x: 20,
            y: 30,
            width: 100,
            height: 40,
        })),
    );

    assert!(backend
        .verify_observed_click_action_freshness(&action)
        .unwrap_err()
        .contains("stale"));
}

#[test]
fn bounds_free_non_click_actions_fail_closed() {
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
        let mut observed_node = node(bounds);
        observed_node.actions[0].name = "activate".to_string();
        let observation_id = record(
            &backend,
            AccessibilitySnapshotTarget::Window {
                window_id: 42,
                pid: Some(4242),
            },
            observed_node,
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
