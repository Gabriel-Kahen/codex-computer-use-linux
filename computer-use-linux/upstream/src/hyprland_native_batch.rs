use serde::{Deserialize, Serialize};
use std::env;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const PLUGIN_VERSION: &str = "0.1.4";
const BATCH_PROTOCOL_VERSION: u32 = 1;
const EXPLICIT_PLUGIN_PATH_ENV: &str = "CODEX_HYPRLAND_TARGET_POINTER_PLUGIN";
const MAX_ERROR_BYTES: usize = 512;

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum NativePointerAction {
    Click {
        x: f64,
        y: f64,
        button: String,
        count: u32,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativePointerBatch {
    pub(crate) window_id: u64,
    pub(crate) actions: Vec<NativePointerAction>,
}

#[derive(Debug, PartialEq)]
pub(crate) enum NativeBatchOutcome {
    Completed,
    Unavailable(String),
    Refused(String),
    SubmittedFailure(String),
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct PluginIdentity {
    plugin_version: String,
    source_sha256: String,
    hyprland_build_sha256: String,
    hyprland_build_abi: String,
    hyprland_runtime_abi: String,
}

#[derive(Debug, Deserialize)]
struct PluginStatus {
    ok: bool,
    #[serde(flatten)]
    identity: PluginIdentity,
    batch_protocol_version: u32,
    identity_token: String,
}

#[derive(Debug, Deserialize)]
struct BatchReply {
    ok: bool,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    batch_protocol_version: Option<u32>,
    #[serde(default)]
    identity: Option<PluginIdentity>,
    #[serde(default)]
    address: Option<String>,
    #[serde(default)]
    completed: Option<usize>,
    #[serde(default)]
    observed_physical_state_unchanged: Option<bool>,
}

pub(crate) async fn run_native_pointer_batch(batch: NativePointerBatch) -> NativeBatchOutcome {
    match tokio::task::spawn_blocking(move || run_native_pointer_batch_blocking(&batch)).await {
        Ok(outcome) => outcome,
        Err(error) => NativeBatchOutcome::SubmittedFailure(format!(
            "native Hyprland batch worker failed without a trustworthy submission result: {error}"
        )),
    }
}

fn run_native_pointer_batch_blocking(batch: &NativePointerBatch) -> NativeBatchOutcome {
    let status = match ensure_plugin_status() {
        Ok(status) => status,
        Err(PluginSetupError::Unavailable(message)) => {
            return NativeBatchOutcome::Unavailable(message);
        }
        Err(PluginSetupError::Refused(message)) => return NativeBatchOutcome::Refused(message),
    };
    let args = encode_batch(batch, &status.identity_token);
    let output = match Command::new("hyprctl").args(args).output() {
        Ok(output) => output,
        Err(error) => {
            return NativeBatchOutcome::Unavailable(format!(
                "failed to start hyprctl before native batch submission: {error}"
            ));
        }
    };
    // From this point onward, the compositor may have received part or all of
    // the transaction. Never replay through uinput, a portal, or ydotool.
    match validate_batch_reply(output, batch, &status.identity) {
        Ok(()) => NativeBatchOutcome::Completed,
        Err(error) => NativeBatchOutcome::SubmittedFailure(error),
    }
}

enum PluginSetupError {
    Unavailable(String),
    Refused(String),
}

fn ensure_plugin_status() -> Result<PluginStatus, PluginSetupError> {
    match query_plugin_status() {
        Ok(status) => validate_plugin_status(status),
        Err(error) if is_unknown_request(&error) => {
            let Some(path) = env::var_os(EXPLICIT_PLUGIN_PATH_ENV).map(PathBuf::from) else {
                return Err(PluginSetupError::Unavailable(format!(
                    "Hyprland native batch command is not loaded; start the same-session companion first or set {EXPLICIT_PLUGIN_PATH_ENV} to a compiled plugin"
                )));
            };
            validate_plugin_path(&path).map_err(PluginSetupError::Refused)?;
            let loaded = Command::new("hyprctl")
                .args(["plugin", "load"])
                .arg(&path)
                .output()
                .map_err(|load_error| {
                    PluginSetupError::Unavailable(format!(
                        "failed to start hyprctl before plugin loading: {load_error}"
                    ))
                })?;
            if !loaded.status.success()
                || !String::from_utf8_lossy(&loaded.stdout)
                    .to_ascii_lowercase()
                    .contains("ok")
            {
                return Err(PluginSetupError::Refused(format!(
                    "Hyprland refused the explicit native plugin: {}",
                    output_message(&loaded)
                )));
            }
            validate_plugin_status(query_plugin_status().map_err(PluginSetupError::Refused)?)
        }
        Err(error) if error.starts_with("failed to start Hyprland native status probe:") => {
            Err(PluginSetupError::Unavailable(error))
        }
        Err(error) => Err(PluginSetupError::Refused(format!(
            "Hyprland native plugin status was present but could not be trusted: {error}"
        ))),
    }
}

fn query_plugin_status() -> Result<PluginStatus, String> {
    let output = Command::new("hyprctl")
        .args(["-j", "cutargetstatus"])
        .output()
        .map_err(|error| format!("failed to start Hyprland native status probe: {error}"))?;
    if !output.status.success() {
        return Err(output_message(&output));
    }
    match serde_json::from_slice(&output.stdout) {
        Ok(status) => Ok(status),
        Err(error) => {
            let message = output_message(&output);
            if is_unknown_request(&message) {
                Err(message)
            } else {
                Err(format!(
                    "Hyprland native status returned invalid JSON: {error}; output: {message}"
                ))
            }
        }
    }
}

fn validate_plugin_status(status: PluginStatus) -> Result<PluginStatus, PluginSetupError> {
    let expected_token = identity_token(&status.identity);
    if !status.ok
        || status.identity.plugin_version != PLUGIN_VERSION
        || status.batch_protocol_version != BATCH_PROTOCOL_VERSION
        || status.identity.hyprland_build_abi != status.identity.hyprland_runtime_abi
        || !is_sha256(&status.identity.source_sha256)
        || !is_sha256(&status.identity.hyprland_build_sha256)
        || status.identity_token != expected_token
    {
        return Err(PluginSetupError::Refused(
            "loaded Hyprland native batch plugin has an incompatible protocol, identity, or ABI"
                .to_string(),
        ));
    }
    Ok(status)
}

fn validate_plugin_path(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err(format!(
            "{EXPLICIT_PLUGIN_PATH_ENV} must be an absolute path"
        ));
    }
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect explicit Hyprland plugin path: {error}"))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("explicit Hyprland plugin path must be a regular non-symlink file".to_string());
    }
    if path.extension() != Some(OsStr::new("so")) {
        return Err("explicit Hyprland plugin path must have a .so extension".to_string());
    }
    Ok(())
}

fn encode_batch(batch: &NativePointerBatch, identity_token: &str) -> Vec<String> {
    let mut args = vec![
        "-j".to_string(),
        "cutargetbatch".to_string(),
        format!("v{BATCH_PROTOCOL_VERSION}"),
        identity_token.to_string(),
        format!("0x{:x}", batch.window_id),
        batch.actions.len().to_string(),
    ];
    for action in &batch.actions {
        match action {
            NativePointerAction::Click {
                x,
                y,
                button,
                count,
            } => args.extend([
                "click".to_string(),
                x.to_string(),
                y.to_string(),
                button.clone(),
                count.to_string(),
            ]),
        }
    }
    args
}

fn validate_batch_reply(
    output: Output,
    batch: &NativePointerBatch,
    expected_identity: &PluginIdentity,
) -> Result<(), String> {
    if !output.status.success() {
        return Err(format!(
            "native Hyprland batch transport failed after submission: {}",
            output_message(&output)
        ));
    }
    let reply: BatchReply = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("native Hyprland batch returned invalid JSON: {error}"))?;
    if !reply.ok {
        return Err(bounded_message(
            reply
                .error
                .as_deref()
                .unwrap_or("native Hyprland batch was rejected after submission"),
        ));
    }
    let expected_address = format!("0x{:x}", batch.window_id);
    if reply.batch_protocol_version != Some(BATCH_PROTOCOL_VERSION)
        || reply.identity.as_ref() != Some(expected_identity)
        || reply.address.as_deref() != Some(expected_address.as_str())
        || reply.completed != Some(batch.actions.len())
        || reply.observed_physical_state_unchanged != Some(true)
    {
        return Err("native Hyprland batch reply identity or completion proof mismatched the submitted transaction".to_string());
    }
    Ok(())
}

fn identity_token(identity: &PluginIdentity) -> String {
    format!(
        "v1.{}.{}.{}",
        identity.plugin_version, identity.source_sha256, identity.hyprland_build_sha256
    )
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn is_unknown_request(message: &str) -> bool {
    message.to_ascii_lowercase().contains("unknown request")
}

fn output_message(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    bounded_message(if stderr.trim().is_empty() {
        stdout.trim()
    } else {
        stderr.trim()
    })
}

fn bounded_message(message: &str) -> String {
    let mut end = message.len().min(MAX_ERROR_BYTES);
    while !message.is_char_boundary(end) {
        end -= 1;
    }
    message[..end]
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .collect()
}

#[cfg(test)]
#[path = "hyprland_native_batch_tests.rs"]
mod tests;
