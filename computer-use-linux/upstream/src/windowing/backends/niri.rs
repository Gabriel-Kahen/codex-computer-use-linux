use crate::terminal::enrich_terminal_windows;
use crate::windowing::registry::BackendProbe;
use crate::windowing::types::{WindowBounds, WindowInfo};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::process::Command;

use super::niri_ipc;

pub const NIRI_BACKEND: &str = "niri";

pub fn probe() -> BackendProbe {
    if let Ok(windows) = niri_ipc::cached_windows() {
        return BackendProbe {
            id: NIRI_BACKEND,
            ok: true,
            can_list_windows: true,
            can_focus_apps: true,
            can_focus_windows: true,
            detail: format!(
                "Niri IPC event stream returned an initial snapshot with {} window(s)",
                windows.len()
            ),
        };
    }

    match niri_output(&["msg", "--json", "windows"]) {
        Ok(output) if output.status.success() => {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let ok = matches!(
                serde_json::from_str::<serde_json::Value>(&stdout),
                Ok(serde_json::Value::Array(_))
            );
            BackendProbe {
                id: NIRI_BACKEND,
                ok,
                can_list_windows: ok,
                can_focus_apps: ok,
                can_focus_windows: ok,
                detail: if ok {
                    "niri msg --json windows returned a JSON array".to_string()
                } else {
                    "niri msg --json windows did not return a JSON array".to_string()
                },
            }
        }
        Ok(output) => BackendProbe {
            id: NIRI_BACKEND,
            ok: false,
            can_list_windows: false,
            can_focus_apps: false,
            can_focus_windows: false,
            detail: command_failure_detail(&output),
        },
        Err(error) => BackendProbe {
            id: NIRI_BACKEND,
            ok: false,
            can_list_windows: false,
            can_focus_apps: false,
            can_focus_windows: false,
            detail: error.to_string(),
        },
    }
}

pub fn list_windows() -> Result<Vec<WindowInfo>> {
    if let Ok(records) = niri_ipc::cached_windows() {
        return windows_from_records(records);
    }

    let output = niri_output(&["msg", "--json", "windows"])
        .context("failed to run niri msg --json windows")?;
    if !output.status.success() {
        bail!(
            "niri msg --json windows failed: {}",
            command_failure_detail(&output)
        );
    }

    parse_niri_windows(&String::from_utf8_lossy(&output.stdout))
}

pub(crate) fn parse_niri_windows(json: &str) -> Result<Vec<WindowInfo>> {
    let records: Vec<NiriWindow> =
        serde_json::from_str(json).context("failed to parse niri msg --json windows output")?;
    windows_from_records(records)
}

fn windows_from_records(records: Vec<NiriWindow>) -> Result<Vec<WindowInfo>> {
    let mut windows = records
        .into_iter()
        .map(WindowInfo::from)
        .collect::<Vec<_>>();
    windows.sort_by_key(|window| window.window_id);
    enrich_terminal_windows(&mut windows);
    Ok(windows)
}

pub fn activate_window(window_id: u64) -> Result<()> {
    if niri_ipc::focus_window(window_id).is_ok() {
        return Ok(());
    }

    let args = niri_focus_args(window_id);
    let output = niri_output(&args.iter().map(String::as_str).collect::<Vec<_>>())
        .with_context(|| format!("failed to focus Niri window {window_id}"))?;
    if output.status.success() {
        Ok(())
    } else {
        bail!(
            "niri msg action focus-window --id {window_id} failed: {}",
            command_failure_detail(&output)
        );
    }
}

pub(crate) fn niri_focus_args(window_id: u64) -> [String; 5] {
    [
        "msg".to_string(),
        "action".to_string(),
        "focus-window".to_string(),
        "--id".to_string(),
        window_id.to_string(),
    ]
}

fn niri_output(args: &[&str]) -> std::io::Result<std::process::Output> {
    Command::new("niri").args(args).output()
}

fn command_failure_detail(output: &std::process::Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if stderr.is_empty() {
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    } else {
        stderr
    }
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct NiriWindow {
    pub(super) id: u64,
    title: Option<String>,
    app_id: Option<String>,
    pid: Option<i64>,
    workspace_id: Option<u64>,
    #[serde(default)]
    pub(super) is_focused: bool,
    pub(super) layout: Option<NiriWindowLayout>,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct NiriWindowLayout {
    pub(super) window_size: Option<[i64; 2]>,
}

impl From<NiriWindow> for WindowInfo {
    fn from(window: NiriWindow) -> Self {
        let bounds = window.layout.and_then(|layout| {
            let [width, height] = layout.window_size?;
            let width = u32::try_from(width).ok().filter(|value| *value > 0)?;
            let height = u32::try_from(height).ok().filter(|value| *value > 0)?;
            Some(WindowBounds {
                x: None,
                y: None,
                width,
                height,
            })
        });

        Self {
            window_id: window.id,
            title: window.title,
            app_id: window.app_id.clone(),
            wm_class: window.app_id,
            pid: window.pid.and_then(|pid| u32::try_from(pid).ok()),
            bounds,
            workspace: window
                .workspace_id
                .and_then(|workspace| i32::try_from(workspace).ok()),
            focused: window.is_focused,
            hidden: false,
            client_type: None,
            backend: NIRI_BACKEND.to_string(),
            terminal: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_nullable_metadata_without_inventing_global_coordinates() {
        let windows = parse_niri_windows(
            r#"[{"id":42,"title":null,"app_id":"org.example.App","pid":123,"workspace_id":7,"is_focused":true,"layout":{"window_size":[900,700]}}]"#,
        )
        .unwrap();

        assert_eq!(
            serde_json::to_value(windows).unwrap(),
            serde_json::to_value(vec![WindowInfo {
                window_id: 42,
                title: None,
                app_id: Some("org.example.App".to_string()),
                wm_class: Some("org.example.App".to_string()),
                pid: Some(123),
                bounds: Some(WindowBounds {
                    x: None,
                    y: None,
                    width: 900,
                    height: 700,
                }),
                workspace: Some(7),
                focused: true,
                hidden: false,
                client_type: None,
                backend: NIRI_BACKEND.to_string(),
                terminal: None,
            }])
            .unwrap()
        );
    }

    #[test]
    fn rejects_unsafe_numeric_values() {
        let windows = parse_niri_windows(
            r#"[{"id":1,"title":"bad","app_id":null,"pid":-1,"workspace_id":9223372036854775807,"is_focused":false,"layout":{"window_size":[-20,0]}}]"#,
        )
        .unwrap();

        assert_eq!(windows[0].pid, None);
        assert_eq!(windows[0].workspace, None);
        assert!(windows[0].bounds.is_none());
    }

    #[test]
    fn builds_focus_command() {
        assert_eq!(
            niri_focus_args(42),
            ["msg", "action", "focus-window", "--id", "42"].map(str::to_string)
        );
    }
}
