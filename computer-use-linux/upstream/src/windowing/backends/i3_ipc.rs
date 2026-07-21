//! Persistent transport for the i3 IPC protocol.

use anyhow::{bail, Context, Result};
use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const IPC_MAGIC: &[u8; 6] = b"i3-ipc";
const IPC_HEADER_LEN: usize = 14;
const IPC_COMMAND: u32 = 0;
const IPC_SUBSCRIBE: u32 = 2;
const IPC_GET_TREE: u32 = 4;
const IPC_EVENT_MASK: u32 = 1 << 31;
const MAX_IPC_PAYLOAD: usize = 32 * 1024 * 1024;
const IPC_TIMEOUT: Duration = Duration::from_secs(2);
const TREE_CACHE_TTL: Duration = Duration::from_millis(250);
const EVENT_SUBSCRIPTIONS: &[u8] = br#"["window","workspace","output","shutdown"]"#;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SocketIdentity {
    device: u64,
    inode: u64,
}

impl SocketIdentity {
    fn read(path: &Path) -> Result<Self> {
        let metadata = fs::metadata(path)
            .with_context(|| format!("failed to inspect i3 IPC socket {}", path.display()))?;
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
}

#[derive(Clone)]
struct CachedTree {
    captured_at: Instant,
    generation: u64,
    payload: Vec<u8>,
}

#[derive(Debug, Eq, PartialEq)]
struct IpcMessage {
    message_type: u32,
    payload: Vec<u8>,
}

pub(super) struct I3Ipc {
    stream: UnixStream,
    socket_path: PathBuf,
    socket_identity: SocketIdentity,
    read_buffer: Vec<u8>,
    dirty: bool,
    event_generation: u64,
    next_generation: u64,
    tree: Option<CachedTree>,
}

impl I3Ipc {
    pub(super) fn connect(socket_path: PathBuf) -> Result<Self> {
        let socket_identity = SocketIdentity::read(&socket_path)?;
        let stream = UnixStream::connect(&socket_path).with_context(|| {
            format!(
                "failed to connect to i3 IPC socket {}",
                socket_path.display()
            )
        })?;
        stream
            .set_read_timeout(Some(IPC_TIMEOUT))
            .context("failed to set i3 IPC read timeout")?;
        stream
            .set_write_timeout(Some(IPC_TIMEOUT))
            .context("failed to set i3 IPC write timeout")?;
        let mut session = Self {
            stream,
            socket_path,
            socket_identity,
            read_buffer: Vec::new(),
            dirty: true,
            event_generation: 0,
            next_generation: 1,
            tree: None,
        };
        let reply = session.request(IPC_SUBSCRIBE, EVENT_SUBSCRIPTIONS)?;
        let accepted = serde_json::from_slice::<serde_json::Value>(&reply)
            .ok()
            .and_then(|value| value.get("success").and_then(serde_json::Value::as_bool))
            == Some(true);
        if !accepted {
            bail!("i3 rejected the IPC event subscription");
        }
        Ok(session)
    }

    pub(super) fn is_current_socket(&self, socket_path: &Path) -> bool {
        self.socket_path == socket_path
            && SocketIdentity::read(socket_path)
                .is_ok_and(|identity| identity == self.socket_identity)
    }

    pub(super) fn refresh_tree(&mut self) -> Result<u64> {
        self.drain_available_events()?;
        let now = Instant::now();
        if let Some(generation) = (!self.dirty)
            .then_some(self.tree.as_ref())
            .flatten()
            .filter(|tree| now.saturating_duration_since(tree.captured_at) < TREE_CACHE_TTL)
            .map(|tree| tree.generation)
        {
            return Ok(generation);
        }

        let event_generation = self.event_generation;
        let payload = self.request(IPC_GET_TREE, &[])?;
        let generation = self.next_generation;
        self.next_generation = self.next_generation.wrapping_add(1).max(1);
        self.tree = Some(CachedTree {
            captured_at: now,
            generation,
            payload,
        });
        // An event can overtake a GET_TREE reply after subscription. If that
        // happens, conservatively refresh again on the next call rather than
        // claiming the reply includes the event's state transition.
        self.dirty = self.event_generation != event_generation;
        Ok(generation)
    }

    pub(super) fn tree_payload(&self) -> Result<&[u8]> {
        self.tree
            .as_ref()
            .map(|tree| tree.payload.as_slice())
            .context("i3 tree has not been requested")
    }

    pub(super) fn command(&mut self, command: &str) -> Result<Vec<u8>> {
        self.drain_available_events()?;
        let reply = self.request(IPC_COMMAND, command.as_bytes())?;
        self.dirty = true;
        Ok(reply)
    }

    fn request(&mut self, message_type: u32, payload: &[u8]) -> Result<Vec<u8>> {
        let frame = encode_frame(message_type, payload)?;
        self.stream
            .write_all(&frame)
            .context("failed to write i3 IPC request")?;
        loop {
            let message = self.read_message()?;
            if is_event(message.message_type) {
                self.note_event();
                continue;
            }
            if message.message_type != message_type {
                bail!(
                    "i3 IPC returned message type {} while waiting for {message_type}",
                    message.message_type
                );
            }
            return Ok(message.payload);
        }
    }

    fn drain_available_events(&mut self) -> Result<()> {
        self.stream
            .set_nonblocking(true)
            .context("failed to make i3 IPC socket nonblocking")?;
        let mut read_result = Ok(());
        loop {
            match decode_frame(&mut self.read_buffer) {
                Ok(Some(message)) if is_event(message.message_type) => {
                    self.note_event();
                    continue;
                }
                Ok(Some(message)) => {
                    read_result = Err(anyhow::anyhow!(
                        "unexpected queued i3 IPC response of type {}",
                        message.message_type
                    ));
                    break;
                }
                Ok(None) => {}
                Err(error) => {
                    read_result = Err(error);
                    break;
                }
            }

            let mut chunk = [0_u8; 8192];
            match self.stream.read(&mut chunk) {
                Ok(0) => {
                    read_result = Err(anyhow::anyhow!("i3 IPC socket closed"));
                    break;
                }
                Ok(count) => self.read_buffer.extend_from_slice(&chunk[..count]),
                Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                Err(error) => {
                    read_result = Err(error).context("failed to drain i3 IPC events");
                    break;
                }
            }
        }
        let restore_result = self
            .stream
            .set_nonblocking(false)
            .context("failed to restore blocking i3 IPC socket");
        read_result?;
        restore_result?;
        Ok(())
    }

    fn read_message(&mut self) -> Result<IpcMessage> {
        loop {
            if let Some(message) = decode_frame(&mut self.read_buffer)? {
                return Ok(message);
            }
            let mut chunk = [0_u8; 8192];
            let count = self
                .stream
                .read(&mut chunk)
                .context("failed to read i3 IPC response")?;
            if count == 0 {
                bail!("i3 IPC socket closed while waiting for a response");
            }
            self.read_buffer.extend_from_slice(&chunk[..count]);
        }
    }

    fn note_event(&mut self) {
        self.dirty = true;
        self.event_generation = self.event_generation.wrapping_add(1);
    }
}

fn is_event(message_type: u32) -> bool {
    message_type & IPC_EVENT_MASK != 0
}

fn encode_frame(message_type: u32, payload: &[u8]) -> Result<Vec<u8>> {
    if payload.len() > MAX_IPC_PAYLOAD {
        bail!("i3 IPC payload exceeds {MAX_IPC_PAYLOAD} bytes");
    }
    let payload_len = u32::try_from(payload.len()).context("i3 IPC payload exceeds 32 bits")?;
    let mut frame = Vec::with_capacity(IPC_HEADER_LEN + payload.len());
    frame.extend_from_slice(IPC_MAGIC);
    frame.extend_from_slice(&payload_len.to_ne_bytes());
    frame.extend_from_slice(&message_type.to_ne_bytes());
    frame.extend_from_slice(payload);
    Ok(frame)
}

fn decode_frame(buffer: &mut Vec<u8>) -> Result<Option<IpcMessage>> {
    if buffer.len() < IPC_HEADER_LEN {
        return Ok(None);
    }
    if &buffer[..IPC_MAGIC.len()] != IPC_MAGIC {
        bail!("i3 IPC response has invalid magic bytes");
    }
    let payload_len = u32::from_ne_bytes(
        buffer[6..10]
            .try_into()
            .expect("i3 IPC length has four bytes"),
    ) as usize;
    if payload_len > MAX_IPC_PAYLOAD {
        bail!("i3 IPC response exceeds {MAX_IPC_PAYLOAD} bytes");
    }
    let frame_len = IPC_HEADER_LEN + payload_len;
    if buffer.len() < frame_len {
        return Ok(None);
    }
    let message_type = u32::from_ne_bytes(
        buffer[10..14]
            .try_into()
            .expect("i3 IPC type has four bytes"),
    );
    let payload = buffer[IPC_HEADER_LEN..frame_len].to_vec();
    buffer.drain(..frame_len);
    Ok(Some(IpcMessage {
        message_type,
        payload,
    }))
}

#[cfg(test)]
#[path = "i3_ipc_tests.rs"]
mod tests;
