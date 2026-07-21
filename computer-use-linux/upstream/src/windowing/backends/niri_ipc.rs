use super::niri::{NiriWindow, NiriWindowLayout};
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const INITIAL_SNAPSHOT_TIMEOUT: Duration = Duration::from_millis(350);
const SOCKET_POLL_INTERVAL: Duration = Duration::from_millis(250);
const RECONNECT_DELAY: Duration = Duration::from_millis(100);

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
struct SocketIdentity {
    device: u64,
    inode: u64,
}

#[derive(Debug, Default)]
struct EventState {
    windows: BTreeMap<u64, NiriWindow>,
    ready: bool,
}

impl EventState {
    fn apply_line(&mut self, line: &str) -> Result<bool> {
        let event: Value = serde_json::from_str(line).context("invalid Niri event-stream JSON")?;
        let Some(event) = event.as_object() else {
            return Ok(false);
        };

        if let Some(payload) = event.get("WindowsChanged") {
            let windows: WindowsChanged = serde_json::from_value(payload.clone())
                .context("invalid Niri WindowsChanged event")?;
            self.windows = windows
                .windows
                .into_iter()
                .map(|window| (window.id, window))
                .collect();
            self.ready = true;
            return Ok(true);
        }

        if let Some(payload) = event.get("WindowOpenedOrChanged") {
            let changed: WindowChanged = serde_json::from_value(payload.clone())
                .context("invalid Niri WindowOpenedOrChanged event")?;
            if changed.window.is_focused {
                self.clear_focus();
            }
            self.windows.insert(changed.window.id, changed.window);
            return Ok(true);
        }

        if let Some(payload) = event.get("WindowClosed") {
            let closed: WindowClosed = serde_json::from_value(payload.clone())
                .context("invalid Niri WindowClosed event")?;
            self.windows.remove(&closed.id);
            return Ok(true);
        }

        if let Some(payload) = event.get("WindowFocusChanged") {
            let focused: WindowFocusChanged = serde_json::from_value(payload.clone())
                .context("invalid Niri WindowFocusChanged event")?;
            self.clear_focus();
            if let Some(window) = focused.id.and_then(|id| self.windows.get_mut(&id)) {
                window.is_focused = true;
            }
            return Ok(true);
        }

        if let Some(payload) = event.get("WindowLayoutsChanged") {
            let layouts: WindowLayoutsChanged = serde_json::from_value(payload.clone())
                .context("invalid Niri WindowLayoutsChanged event")?;
            for (id, layout) in layouts.changes {
                if let Some(window) = self.windows.get_mut(&id) {
                    window.layout = Some(layout);
                }
            }
            return Ok(true);
        }

        // Event is valid but unrelated to window state, or comes from a newer Niri release.
        Ok(false)
    }

    fn clear_focus(&mut self) {
        for window in self.windows.values_mut() {
            window.is_focused = false;
        }
    }
}

#[derive(serde::Deserialize)]
struct WindowsChanged {
    windows: Vec<NiriWindow>,
}

#[derive(serde::Deserialize)]
struct WindowChanged {
    window: NiriWindow,
}

#[derive(serde::Deserialize)]
struct WindowClosed {
    id: u64,
}

#[derive(serde::Deserialize)]
struct WindowFocusChanged {
    id: Option<u64>,
}

#[derive(serde::Deserialize)]
struct WindowLayoutsChanged {
    changes: Vec<(u64, NiriWindowLayout)>,
}

struct SharedState {
    state: Mutex<EventState>,
    changed: Condvar,
    stop: AtomicBool,
}

impl Default for SharedState {
    fn default() -> Self {
        Self {
            state: Mutex::new(EventState::default()),
            changed: Condvar::new(),
            stop: AtomicBool::new(false),
        }
    }
}

struct EventClient {
    socket_path: PathBuf,
    shared: Arc<SharedState>,
}

impl EventClient {
    fn start(socket_path: PathBuf) -> Self {
        let shared = Arc::new(SharedState::default());
        let worker_shared = Arc::clone(&shared);
        let worker_path = socket_path.clone();
        thread::Builder::new()
            .name("computer-use-niri-ipc".to_string())
            .spawn(move || event_worker(&worker_path, &worker_shared))
            .expect("failed to start Niri IPC event worker");
        Self {
            socket_path,
            shared,
        }
    }

    fn snapshot(&self, timeout: Duration) -> Result<Vec<NiriWindow>> {
        let deadline = Instant::now() + timeout;
        let mut state = lock_state(&self.shared);
        while !state.ready {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                bail!(
                    "Niri event stream did not provide an initial window snapshot within {} ms",
                    timeout.as_millis()
                );
            }
            let (next, wait) = self
                .shared
                .changed
                .wait_timeout(state, remaining)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state = next;
            if wait.timed_out() && !state.ready {
                bail!(
                    "Niri event stream did not provide an initial window snapshot within {} ms",
                    timeout.as_millis()
                );
            }
        }
        Ok(state.windows.values().cloned().collect())
    }

    fn stop(&self) {
        self.shared.stop.store(true, Ordering::Release);
        self.shared.changed.notify_all();
    }
}

fn lock_state(shared: &SharedState) -> MutexGuard<'_, EventState> {
    shared
        .state
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn event_worker(socket_path: &Path, shared: &SharedState) {
    while !shared.stop.load(Ordering::Acquire) {
        mark_disconnected(shared);
        if consume_event_stream(socket_path, shared).is_ok() {
            continue;
        }
        if shared.stop.load(Ordering::Acquire) {
            break;
        }
        thread::sleep(RECONNECT_DELAY);
    }
}

fn mark_disconnected(shared: &SharedState) {
    let mut state = lock_state(shared);
    state.ready = false;
    state.windows.clear();
    shared.changed.notify_all();
}

fn consume_event_stream(socket_path: &Path, shared: &SharedState) -> Result<()> {
    let identity = socket_identity(socket_path).with_context(|| {
        format!(
            "failed to identify Niri IPC socket {}",
            socket_path.display()
        )
    })?;
    let mut stream = UnixStream::connect(socket_path).with_context(|| {
        format!(
            "failed to connect to Niri IPC socket {}",
            socket_path.display()
        )
    })?;
    stream
        .set_read_timeout(Some(SOCKET_POLL_INTERVAL))
        .context("failed to configure Niri IPC socket timeout")?;
    stream
        .write_all(b"\"EventStream\"\n")
        .context("failed to request Niri event stream")?;
    stream.flush().context("failed to flush Niri IPC request")?;

    let mut reader = BufReader::new(stream);
    let reply = read_required_line(&mut reader, shared, socket_path, identity)?;
    ensure_handled_reply(reply.trim())?;

    loop {
        if shared.stop.load(Ordering::Acquire) {
            return Ok(());
        }
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => bail!("Niri event stream closed"),
            Ok(_) => {
                let changed = {
                    let mut state = lock_state(shared);
                    state.apply_line(line.trim_end())?
                };
                if changed {
                    shared.changed.notify_all();
                }
            }
            Err(error) if is_timeout(&error) => {
                if socket_identity(socket_path).ok() != Some(identity) {
                    bail!("Niri IPC socket was replaced");
                }
            }
            Err(error) => return Err(error).context("failed to read Niri event stream"),
        }
    }
}

fn read_required_line(
    reader: &mut BufReader<UnixStream>,
    shared: &SharedState,
    socket_path: &Path,
    identity: SocketIdentity,
) -> Result<String> {
    loop {
        if shared.stop.load(Ordering::Acquire) {
            bail!("Niri IPC event worker stopped");
        }
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => bail!("Niri IPC socket closed before replying"),
            Ok(_) => return Ok(line),
            Err(error) if is_timeout(&error) => {
                if socket_identity(socket_path).ok() != Some(identity) {
                    bail!("Niri IPC socket was replaced before replying");
                }
            }
            Err(error) => return Err(error).context("failed to read Niri IPC reply"),
        }
    }
}

fn is_timeout(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
    )
}

fn socket_identity(path: &Path) -> std::io::Result<SocketIdentity> {
    let metadata = fs::metadata(path)?;
    Ok(SocketIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

fn ensure_handled_reply(line: &str) -> Result<()> {
    let reply: Value = serde_json::from_str(line).context("invalid Niri IPC reply JSON")?;
    if reply == serde_json::json!({"Ok": "Handled"}) {
        return Ok(());
    }
    if let Some(error) = reply.get("Err").and_then(Value::as_str) {
        bail!("Niri IPC request failed: {error}");
    }
    bail!("unexpected Niri IPC reply: {reply}")
}

fn manager() -> &'static Mutex<Option<EventClient>> {
    static MANAGER: OnceLock<Mutex<Option<EventClient>>> = OnceLock::new();
    MANAGER.get_or_init(|| Mutex::new(None))
}

fn socket_path_from_env() -> Result<PathBuf> {
    env::var_os("NIRI_SOCKET")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("NIRI_SOCKET is not set"))
}

pub(super) fn cached_windows() -> Result<Vec<NiriWindow>> {
    let socket_path = socket_path_from_env()?;
    let mut manager = manager()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if manager
        .as_ref()
        .is_none_or(|client| client.socket_path != socket_path)
    {
        if let Some(client) = manager.as_ref() {
            client.stop();
        }
        *manager = Some(EventClient::start(socket_path));
    }
    manager
        .as_ref()
        .expect("Niri event client should be initialized")
        .snapshot(INITIAL_SNAPSHOT_TIMEOUT)
}

pub(super) fn focus_window(window_id: u64) -> Result<()> {
    let socket_path = socket_path_from_env()?;
    let request = serde_json::json!({
        "Action": {
            "FocusWindow": {
                "id": window_id
            }
        }
    });
    send_request(&socket_path, &request)
}

fn send_request(socket_path: &Path, request: &Value) -> Result<()> {
    let mut stream = UnixStream::connect(socket_path).with_context(|| {
        format!(
            "failed to connect to Niri IPC socket {}",
            socket_path.display()
        )
    })?;
    stream
        .set_read_timeout(Some(INITIAL_SNAPSHOT_TIMEOUT))
        .context("failed to configure Niri IPC command timeout")?;
    serde_json::to_writer(&mut stream, request).context("failed to encode Niri IPC request")?;
    stream
        .write_all(b"\n")
        .context("failed to terminate Niri IPC request")?;
    stream.flush().context("failed to flush Niri IPC request")?;
    let mut reply = String::new();
    BufReader::new(stream)
        .read_line(&mut reply)
        .context("failed to read Niri IPC reply")?;
    ensure_handled_reply(reply.trim())
}

#[cfg(test)]
#[path = "niri_ipc_tests.rs"]
mod tests;
