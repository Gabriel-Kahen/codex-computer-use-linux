use super::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static NEXT_TEST_DIR: AtomicU64 = AtomicU64::new(1);

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let sequence = NEXT_TEST_DIR.fetch_add(1, Ordering::Relaxed);
        let path = PathBuf::from("/tmp").join(format!(
            "cu-cosmic-{}-{unique}-{sequence}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn persistent_manager_reuses_one_helper_for_multiple_requests() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("fake-cosmic-helper");
    let starts = temporary.0.join("starts");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" >> '{}'\nwhile IFS= read -r line; do\n  id=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\\([0-9][0-9]*\\).*/\\1/p')\n  printf '{{\"version\":1,\"id\":%s,\"ok\":true,\"result\":{{\"served\":true}}}}\\n' \"$id\"\ndone\n",
            starts.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let mut manager = ServiceManager::default();
    manager
        .request(&helper, CosmicServiceCommand::ListWindows)
        .unwrap();
    manager
        .request(&helper, CosmicServiceCommand::FocusedWindow)
        .unwrap();

    assert_eq!(fs::read_to_string(starts).unwrap().lines().count(), 1);
}

#[test]
fn persistent_manager_restarts_a_helper_that_exits_between_requests() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("fake-cosmic-helper");
    let starts = temporary.0.join("starts");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" >> '{}'\nIFS= read -r line || exit 1\nid=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\\([0-9][0-9]*\\).*/\\1/p')\nprintf '{{\"version\":1,\"id\":%s,\"ok\":true,\"result\":{{\"served\":true}}}}\\n' \"$id\"\n",
            starts.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let mut manager = ServiceManager::default();
    let first = manager
        .request(&helper, CosmicServiceCommand::ListWindows)
        .unwrap();
    let second = manager
        .request(&helper, CosmicServiceCommand::FocusedWindow)
        .unwrap();

    assert_eq!(first, serde_json::json!({"served": true}));
    assert_eq!(second, serde_json::json!({"served": true}));
    assert_eq!(fs::read_to_string(starts).unwrap().lines().count(), 2);
}

#[test]
fn rejects_out_of_sequence_service_responses() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("bad-cosmic-helper");
    fs::write(
        &helper,
        "#!/bin/sh\nIFS= read -r line || exit 1\nprintf '{\"version\":1,\"id\":999,\"ok\":true,\"result\":null}\\n'\n",
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let mut persistent = PersistentHelper::spawn(helper).unwrap();
    let error = persistent
        .request(CosmicServiceCommand::Probe)
        .unwrap_err()
        .to_string();

    assert!(error.contains("response id mismatch"));
}

#[test]
fn application_rejection_does_not_restart_the_helper() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("rejecting-cosmic-helper");
    let starts = temporary.0.join("starts");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" >> '{}'\nIFS= read -r line || exit 1\nid=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\\([0-9][0-9]*\\).*/\\1/p')\nprintf '{{\"version\":1,\"id\":%s,\"ok\":false,\"error\":\"minimized\"}}\\n' \"$id\"\n",
            starts.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let error = ServiceManager::default()
        .request(
            &helper,
            CosmicServiceCommand::CaptureWindow {
                window_id: 42,
                output_path: temporary.0.join("capture.png"),
            },
        )
        .unwrap_err();

    assert!(error.downcast_ref::<HelperRejected>().is_some());
    assert_eq!(fs::read_to_string(starts).unwrap().lines().count(), 1);
}

#[test]
fn rejects_unsupported_service_protocol_versions() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("future-cosmic-helper");
    fs::write(
        &helper,
        "#!/bin/sh\nIFS= read -r line || exit 1\nprintf '{\"version\":2,\"id\":1,\"ok\":true,\"result\":null}\\n'\n",
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let mut persistent = PersistentHelper::spawn(helper).unwrap();
    let error = persistent
        .request(CosmicServiceCommand::Probe)
        .unwrap_err()
        .to_string();

    assert!(error.contains("protocol version mismatch"));
}

#[test]
fn persistent_manager_times_out_and_restarts_a_wedged_helper() {
    let temporary = TestDir::new();
    let helper = temporary.0.join("wedged-cosmic-helper");
    let starts = temporary.0.join("starts");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" >> '{}'\nIFS= read -r line || exit 1\nwhile :; do :; done\n",
            starts.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();

    let error = ServiceManager::default()
        .request(&helper, CosmicServiceCommand::Probe)
        .unwrap_err();

    assert!(error.downcast_ref::<HelperTimeout>().is_some());
    assert_eq!(fs::read_to_string(starts).unwrap().lines().count(), 2);
}
