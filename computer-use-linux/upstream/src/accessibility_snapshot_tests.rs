use super::*;

fn node(object_ref: &str) -> AccessibilityNode {
    AccessibilityNode {
        index: 7,
        parent_index: None,
        depth: 0,
        object_ref: object_ref.to_string(),
        role: "push button".to_string(),
        name: None,
        description: None,
        child_count: 0,
        bounds: None,
        states: Vec::new(),
        actions: Vec::new(),
        value: None,
        text: None,
        supports_editable_text: false,
    }
}

fn window(window_id: u64) -> AccessibilitySnapshotTarget {
    AccessibilitySnapshotTarget::Window {
        window_id,
        pid: Some(window_id as u32),
    }
}

#[test]
fn snapshots_are_isolated_by_target() {
    let mut store = AccessibilitySnapshotStore::default();
    let first_id = store.record(window(10), &[node(":1.10/button")]);
    let second_id = store.record(window(20), &[node(":1.20/button")]);

    assert_ne!(first_id, second_id);
    assert_eq!(
        store.resolve(&first_id).unwrap().nodes()[0].object_ref,
        ":1.10/button"
    );
    assert_eq!(
        store.resolve(&second_id).unwrap().nodes()[0].object_ref,
        ":1.20/button"
    );
}

#[test]
fn recording_a_target_replaces_its_previous_generation() {
    let mut store = AccessibilitySnapshotStore::default();
    let stale_id = store.record(window(10), &[node(":1.10/old")]);
    let current_id = store.record(window(10), &[node(":1.10/new")]);

    assert_eq!(store.snapshots.len(), 1);
    assert_ne!(stale_id, current_id);
    assert!(store.resolve(&stale_id).unwrap_err().contains("stale"));
    assert_eq!(
        store.resolve(&current_id).unwrap().nodes()[0].object_ref,
        ":1.10/new"
    );
}

#[test]
fn window_generation_ignores_pid_metadata_changes() {
    let mut store = AccessibilitySnapshotStore::default();
    let stale_id = store.record(
        AccessibilitySnapshotTarget::Window {
            window_id: 10,
            pid: None,
        },
        &[node(":1.10/old")],
    );
    let current_id = store.record(
        AccessibilitySnapshotTarget::Window {
            window_id: 10,
            pid: Some(210),
        },
        &[node(":1.10/new")],
    );

    assert_eq!(store.snapshots.len(), 1);
    assert_ne!(stale_id, current_id);
    assert!(store
        .snapshots
        .iter()
        .all(|snapshot| snapshot.id != stale_id));
    assert_eq!(store.snapshots[0].id, current_id);
}

#[test]
fn target_capacity_evicts_the_oldest_snapshot() {
    let start = Instant::now();
    let mut store = AccessibilitySnapshotStore {
        max_targets: 2,
        ..Default::default()
    };
    let evicted_id = store.record_at(window(10), &[node(":1.10/button")], start);
    let retained_id = store.record_at(window(20), &[node(":1.20/button")], start);
    let newest_id = store.record_at(window(30), &[node(":1.30/button")], start);

    assert_eq!(
        store
            .snapshots
            .iter()
            .map(|snapshot| snapshot.id.as_str())
            .collect::<Vec<_>>(),
        vec![retained_id.as_str(), newest_id.as_str()]
    );
    assert!(store.resolve(&evicted_id).unwrap_err().contains("stale"));
}

#[test]
fn target_ttl_expires_snapshots_only_after_the_boundary() {
    let start = Instant::now();
    let ttl = Duration::from_secs(1);
    let mut store = AccessibilitySnapshotStore {
        max_targets: 4,
        ttl,
        ..Default::default()
    };
    let expiring_id = store.record_at(window(10), &[node(":1.10/button")], start);
    let control_id = store.record_at(
        window(20),
        &[node(":1.20/button")],
        start + Duration::from_millis(500),
    );
    assert_eq!(
        store.resolve_at(&expiring_id, start + ttl).unwrap().nodes()[0].object_ref,
        ":1.10/button"
    );
    assert!(store
        .resolve_at(&expiring_id, start + ttl + Duration::from_nanos(1))
        .unwrap_err()
        .contains("expired"));
    assert_eq!(
        store
            .resolve_at(&control_id, start + ttl + Duration::from_nanos(1))
            .unwrap()
            .nodes()[0]
            .object_ref,
        ":1.20/button"
    );
}
