use crate::cosmic_helper_protocol::{
    CosmicServiceCommand, CosmicServiceRequest, CosmicServiceResponse,
};
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fmt;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{mpsc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use std::{
    env,
    path::{Path, PathBuf},
};

pub const COSMIC_HELPER_BINARY: &str = "computer-use-linux-cosmic";
#[cfg(not(test))]
const SERVICE_RESPONSE_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(test)]
const SERVICE_RESPONSE_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CosmicHelperProbe {
    pub ok: bool,
    pub can_list_windows: bool,
    pub can_activate_windows: bool,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CosmicHelperActivation {
    pub ok: bool,
    pub detail: String,
}

pub fn resolve_helper_binary() -> Result<PathBuf> {
    if let Some(path) = env_var("COMPUTER_USE_LINUX_COSMIC_HELPER") {
        let path = PathBuf::from(path);
        if path.exists() {
            return Ok(path);
        }
    }

    if let Ok(current_exe) = env::current_exe() {
        let sibling = current_exe.with_file_name(COSMIC_HELPER_BINARY);
        if sibling.exists() {
            return Ok(sibling);
        }
    }

    if let Some(path) = command_path(COSMIC_HELPER_BINARY) {
        return Ok(path);
    }

    bail!("COSMIC helper binary {COSMIC_HELPER_BINARY} not found")
}

fn env_var(key: &str) -> Option<String> {
    env::var(key).ok().filter(|value| !value.trim().is_empty())
}

pub fn probe() -> Result<CosmicHelperProbe> {
    run_json_command(CosmicServiceCommand::Probe, vec!["probe".to_string()])
}

pub fn list_windows_json() -> Result<String> {
    let value = run_command(
        CosmicServiceCommand::ListWindows,
        vec!["list-windows".to_string()],
    )?;
    serde_json::to_string(&value).context("failed to serialize COSMIC window list")
}

pub fn focused_window_json() -> Result<String> {
    let value = run_command(
        CosmicServiceCommand::FocusedWindow,
        vec!["focused-window".to_string()],
    )?;
    serde_json::to_string(&value).context("failed to serialize focused COSMIC window")
}

pub fn activate_window(window_id: u64) -> Result<CosmicHelperActivation> {
    run_json_command(
        CosmicServiceCommand::ActivateWindow { window_id },
        vec![
            "activate-window".to_string(),
            "--window-id".to_string(),
            window_id.to_string(),
        ],
    )
}

fn run_json_command<T>(command: CosmicServiceCommand, fallback_args: Vec<String>) -> Result<T>
where
    T: for<'de> Deserialize<'de>,
{
    let output = run_command(command, fallback_args)?;
    serde_json::from_value(output)
        .with_context(|| format!("failed to parse {COSMIC_HELPER_BINARY} JSON output"))
}

fn run_command(command: CosmicServiceCommand, fallback_args: Vec<String>) -> Result<Value> {
    let helper_path = resolve_helper_binary()?;
    let service_result = service_manager()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .request(&helper_path, command);
    match service_result {
        Ok(value) => Ok(value),
        Err(service_error) if service_error.downcast_ref::<HelperTimeout>().is_some() => {
            Err(service_error).context("persistent COSMIC helper timed out after restart")
        }
        Err(service_error) => run_one_shot(&helper_path, &fallback_args).with_context(|| {
            format!("persistent COSMIC helper failed before one-shot fallback: {service_error:#}")
        }),
    }
}

fn run_one_shot(helper: &Path, args: &[String]) -> Result<Value> {
    let output = Command::new(helper)
        .args(args)
        .output()
        .with_context(|| format!("failed to run {}", helper.display()))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let detail = if !stderr.is_empty() { stderr } else { stdout };
        bail!(
            "{} {} failed{}",
            helper.display(),
            args.join(" "),
            if detail.is_empty() {
                String::new()
            } else {
                format!(": {detail}")
            }
        );
    }
    serde_json::from_slice(&output.stdout).with_context(|| {
        format!(
            "{} {} returned invalid JSON",
            helper.display(),
            args.join(" ")
        )
    })
}

#[derive(Default)]
struct ServiceManager {
    helper: Option<PersistentHelper>,
}

impl ServiceManager {
    fn request(&mut self, helper_path: &Path, command: CosmicServiceCommand) -> Result<Value> {
        let mut last_error = None;
        for _ in 0..2 {
            if self
                .helper
                .as_ref()
                .is_some_and(|helper| helper.path != helper_path)
            {
                self.helper = None;
            }
            if self.helper.is_none() {
                match PersistentHelper::spawn(helper_path.to_path_buf()) {
                    Ok(helper) => self.helper = Some(helper),
                    Err(error) => {
                        last_error = Some(error);
                        continue;
                    }
                }
            }

            let result = self
                .helper
                .as_mut()
                .expect("persistent COSMIC helper should be initialized")
                .request(command.clone());
            match result {
                Ok(value) => return Ok(value),
                Err(error) => {
                    last_error = Some(error);
                    self.helper = None;
                }
            }
        }
        Err(last_error.unwrap_or_else(|| anyhow!("persistent COSMIC helper request failed")))
    }
}

fn service_manager() -> &'static Mutex<ServiceManager> {
    static MANAGER: OnceLock<Mutex<ServiceManager>> = OnceLock::new();
    MANAGER.get_or_init(|| Mutex::new(ServiceManager::default()))
}

struct PersistentHelper {
    path: PathBuf,
    child: Child,
    stdin: ChildStdin,
    responses: mpsc::Receiver<HelperOutput>,
    reader: Option<JoinHandle<()>>,
    next_request_id: u64,
}

enum HelperOutput {
    Line(String),
    Eof,
    Error(String),
}

#[derive(Debug)]
struct HelperTimeout;

impl fmt::Display for HelperTimeout {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "persistent COSMIC helper did not respond within {} ms",
            SERVICE_RESPONSE_TIMEOUT.as_millis()
        )
    }
}

impl std::error::Error for HelperTimeout {}

impl PersistentHelper {
    fn spawn(path: PathBuf) -> Result<Self> {
        let mut child = Command::new(&path)
            .arg("serve")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .with_context(|| {
                format!(
                    "failed to start persistent COSMIC helper {}",
                    path.display()
                )
            })?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("persistent COSMIC helper stdin was unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("persistent COSMIC helper stdout was unavailable"))?;
        let (response_tx, responses) = mpsc::channel();
        let reader = thread::spawn(move || {
            let mut stdout = BufReader::new(stdout);
            loop {
                let mut line = String::new();
                match stdout.read_line(&mut line) {
                    Ok(0) => {
                        let _ = response_tx.send(HelperOutput::Eof);
                        break;
                    }
                    Ok(_) => {
                        if response_tx.send(HelperOutput::Line(line)).is_err() {
                            break;
                        }
                    }
                    Err(error) => {
                        let _ = response_tx.send(HelperOutput::Error(error.to_string()));
                        break;
                    }
                }
            }
        });
        Ok(Self {
            path,
            child,
            stdin,
            responses,
            reader: Some(reader),
            next_request_id: 1,
        })
    }

    fn request(&mut self, command: CosmicServiceCommand) -> Result<Value> {
        let id = self.next_request_id;
        self.next_request_id = self.next_request_id.wrapping_add(1).max(1);
        let request = CosmicServiceRequest { id, command };
        serde_json::to_writer(&mut self.stdin, &request)
            .context("failed to encode persistent COSMIC helper request")?;
        self.stdin
            .write_all(b"\n")
            .context("failed to terminate persistent COSMIC helper request")?;
        self.stdin
            .flush()
            .context("failed to flush persistent COSMIC helper request")?;

        let line = match self.responses.recv_timeout(SERVICE_RESPONSE_TIMEOUT) {
            Ok(HelperOutput::Line(line)) => line,
            Ok(HelperOutput::Eof) => bail!("persistent COSMIC helper exited before responding"),
            Ok(HelperOutput::Error(error)) => {
                bail!("failed to read persistent COSMIC helper response: {error}")
            }
            Err(mpsc::RecvTimeoutError::Timeout) => return Err(HelperTimeout.into()),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                bail!("persistent COSMIC helper response reader stopped")
            }
        };
        let response: CosmicServiceResponse = serde_json::from_str(&line)
            .context("persistent COSMIC helper returned invalid response JSON")?;
        if response.id != id {
            bail!(
                "persistent COSMIC helper response id mismatch: expected {id}, received {}",
                response.id
            );
        }
        if !response.ok {
            bail!(
                "persistent COSMIC helper rejected request: {}",
                response
                    .error
                    .as_deref()
                    .unwrap_or("unspecified helper error")
            );
        }
        response
            .result
            .ok_or_else(|| anyhow!("persistent COSMIC helper response omitted its result"))
    }
}

impl Drop for PersistentHelper {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

fn command_path(binary: &str) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    env::split_paths(&path)
        .map(|entry| entry.join(binary))
        .find(|candidate| candidate.is_file() && is_executable(candidate))
}

fn is_executable(path: &Path) -> bool {
    std::fs::metadata(path)
        .map(|metadata| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                metadata.permissions().mode() & 0o111 != 0
            }
            #[cfg(not(unix))]
            {
                metadata.is_file()
            }
        })
        .unwrap_or(false)
}

#[cfg(test)]
#[path = "cosmic_helper_tests.rs"]
mod tests;
