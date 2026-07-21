use super::*;
use serde_json::json;
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

fn fixture() -> (PathBuf, Coordinator) {
    let root = env::temp_dir().join(format!(
        "computer-use-linux-claims-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let binding = BTreeMap::from([
        ("hyprland_instance".to_string(), json!("instance")),
        ("uid".to_string(), json!(1000)),
        ("wayland_display".to_string(), json!("wayland-1")),
        ("xdg_runtime_dir".to_string(), json!("/run/user/1000")),
    ]);
    let coordinator = Coordinator {
        state_dir: root.clone(),
        binding,
    };
    coordinator.prepare_state_dir().unwrap();
    (root, coordinator)
}

fn write_claim(coordinator: &Coordinator, expires_at: f64) {
    let key = coordinator.binding_key();
    let state = json!({
        "version": 1,
        "sessions": {
            key: {
                "binding": coordinator.binding,
                "claims": {
                    "capture:stable-42": {
                        "owner_thread_id": "owner-a",
                        "claim_token": "token-a",
                        "claimed_at": 1.0,
                        "expires_at": expires_at,
                        "lease_seconds": 60,
                        "window": {"address": "0x2a"}
                    }
                }
            }
        }
    });
    fs::write(
        coordinator.state_dir.join("window-claims.json"),
        serde_json::to_vec(&state).unwrap(),
    )
    .unwrap();
}

#[test]
fn binding_key_matches_the_python_coordination_protocol() {
    let (root, coordinator) = fixture();
    assert_eq!(
        coordinator.binding_key(),
        "bd54ddd8dbe6718912f7f91c888b467ef8c01fe55b1ab43459e5d8dc21f5a166"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn companion_process_lock_serializes_a_competing_client_before_rejection() {
    let (root, coordinator) = fixture();
    write_claim(&coordinator, now() + 60.0);
    let lock = coordinator.window_lock_path("address:0x2a");
    let mut companion = Command::new("python3")
        .args([
            "-c",
            "import fcntl,sys; f=open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); input()",
        ])
        .arg(lock)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut ready = String::new();
    BufReader::new(companion.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert_eq!(ready, "locked\n");
    let contender = coordinator.clone();
    let (sent, received) = mpsc::channel();
    let task = thread::spawn(move || {
        sent.send(
            contender
                .acquire(Some(42), &ClaimContext::default())
                .map(|_| ()),
        )
        .unwrap();
    });
    assert!(received.recv_timeout(Duration::from_millis(50)).is_err());
    companion.stdin.take().unwrap().write_all(b"\n").unwrap();
    assert!(companion.wait().unwrap().success());
    let error = received
        .recv_timeout(Duration::from_secs(2))
        .unwrap()
        .unwrap_err();
    assert_eq!(
        error,
        "window is actively claimed by another computer-use agent"
    );
    task.join().unwrap();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn unscoped_capture_is_rejected_while_any_claim_is_live() {
    let (root, coordinator) = fixture();
    write_claim(&coordinator, now() + 60.0);
    let error = coordinator
        .acquire(None, &ClaimContext::default())
        .err()
        .unwrap();
    assert_eq!(
        error,
        "window_id is required for capture while this session has active window claims"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn unclaimed_target_is_rejected_while_another_claim_is_live() {
    let (root, coordinator) = fixture();
    write_claim(&coordinator, now() + 60.0);
    let error = coordinator
        .acquire(Some(43), &ClaimContext::default())
        .err()
        .unwrap();
    assert_eq!(
        error,
        "target window must be claimed while this session has other active window claims"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn expired_claim_does_not_block_a_new_operation() {
    let (root, coordinator) = fixture();
    write_claim(&coordinator, now() - 1.0);
    let guard = coordinator
        .acquire(Some(42), &ClaimContext::default())
        .unwrap();
    drop(guard);
    fs::remove_dir_all(root).unwrap();
}
