use super::*;
use std::os::unix::fs::symlink;
use std::os::unix::process::ExitStatusExt;
use std::time::{SystemTime, UNIX_EPOCH};

fn identity() -> PluginIdentity {
    PluginIdentity {
        plugin_version: PLUGIN_VERSION.to_string(),
        source_sha256: "a".repeat(64),
        hyprland_build_sha256: "b".repeat(64),
        hyprland_build_abi: "abi".to_string(),
        hyprland_runtime_abi: "abi".to_string(),
    }
}

fn batch() -> NativePointerBatch {
    NativePointerBatch {
        window_id: 0xabc,
        actions: vec![NativePointerAction::Click {
            x: 12.5,
            y: 30.0,
            button: "left".to_string(),
            count: 2,
        }],
    }
}

#[test]
fn encodes_a_versioned_single_window_batch() {
    assert_eq!(
        encode_batch(&batch(), "identity"),
        vec![
            "-j",
            "cutargetbatch",
            "v1",
            "identity",
            "0xabc",
            "1",
            "click",
            "12.5",
            "30",
            "left",
            "2",
        ]
    );
}

#[test]
fn accepts_only_a_complete_identity_bound_reply() {
    let identity = identity();
    let reply = serde_json::json!({
        "ok": true,
        "batch_protocol_version": BATCH_PROTOCOL_VERSION,
        "identity": identity,
        "address": "0xabc",
        "completed": 1,
        "observed_physical_state_unchanged": true,
    });
    let output = Output {
        status: std::process::ExitStatus::from_raw(0),
        stdout: serde_json::to_vec(&reply).unwrap(),
        stderr: Vec::new(),
    };

    assert_eq!(validate_batch_reply(output, &batch(), &identity), Ok(()));
}

#[test]
fn rejects_partial_or_identity_mismatched_replies() {
    for reply in [
        serde_json::json!({"ok": false, "error": "failed after delivery"}),
        serde_json::json!({
            "ok": true,
            "batch_protocol_version": BATCH_PROTOCOL_VERSION,
            "identity": identity(),
            "address": "0xabc",
            "completed": 0,
            "observed_physical_state_unchanged": true,
        }),
    ] {
        let output = Output {
            status: std::process::ExitStatus::from_raw(0),
            stdout: serde_json::to_vec(&reply).unwrap(),
            stderr: Vec::new(),
        };
        assert!(validate_batch_reply(output, &batch(), &identity()).is_err());
    }
}

#[test]
fn validates_status_protocol_token_and_abi_together() {
    let identity = identity();
    let status = PluginStatus {
        ok: true,
        identity: identity.clone(),
        batch_protocol_version: BATCH_PROTOCOL_VERSION,
        identity_token: identity_token(&identity),
    };
    assert!(validate_plugin_status(status).is_ok());

    let incompatible = PluginStatus {
        ok: true,
        identity: PluginIdentity {
            hyprland_runtime_abi: "other".to_string(),
            ..identity.clone()
        },
        batch_protocol_version: BATCH_PROTOCOL_VERSION,
        identity_token: identity_token(&identity),
    };
    assert!(validate_plugin_status(incompatible).is_err());
}

#[test]
fn accepts_only_regular_non_symlink_shared_objects() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = env::temp_dir().join(format!("computer-use-plugin-path-{nonce}"));
    fs::create_dir(&directory).unwrap();
    let plugin = directory.join("target-pointer.so");
    fs::write(&plugin, b"plugin").unwrap();
    let wrong_extension = directory.join("target-pointer.dylib");
    fs::write(&wrong_extension, b"plugin").unwrap();
    let link = directory.join("linked.so");
    symlink(&plugin, &link).unwrap();

    assert_eq!(validate_plugin_path(&plugin), Ok(()));
    assert!(validate_plugin_path(&wrong_extension).is_err());
    assert!(validate_plugin_path(&link).is_err());

    fs::remove_dir_all(directory).unwrap();
}
