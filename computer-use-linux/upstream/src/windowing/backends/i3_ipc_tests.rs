use super::*;
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::mpsc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn frame_codec_handles_partial_and_consecutive_messages() {
    let first = encode_frame(IPC_GET_TREE, br#"{"name":"root"}"#).unwrap();
    let second = encode_frame(IPC_EVENT_MASK | 3, br#"{"change":"focus"}"#).unwrap();
    let mut buffer = first[..8].to_vec();
    assert_eq!(decode_frame(&mut buffer).unwrap(), None);
    buffer.extend_from_slice(&first[8..]);
    buffer.extend_from_slice(&second);

    assert_eq!(
        decode_frame(&mut buffer).unwrap(),
        Some(IpcMessage {
            message_type: IPC_GET_TREE,
            payload: br#"{"name":"root"}"#.to_vec(),
        })
    );
    assert!(is_event(
        decode_frame(&mut buffer)
            .unwrap()
            .expect("event frame")
            .message_type
    ));
    assert!(buffer.is_empty());
}

#[test]
fn frame_codec_rejects_invalid_magic_and_oversized_payloads() {
    let mut invalid = encode_frame(IPC_GET_TREE, &[]).unwrap();
    invalid[0] = b'x';
    assert!(decode_frame(&mut invalid).is_err());

    let mut oversized = Vec::from(IPC_MAGIC.as_slice());
    oversized.extend_from_slice(&((MAX_IPC_PAYLOAD as u32) + 1).to_ne_bytes());
    oversized.extend_from_slice(&IPC_GET_TREE.to_ne_bytes());
    assert!(decode_frame(&mut oversized).is_err());
}

#[test]
fn socket_identity_changes_when_path_is_replaced() {
    let socket_path = TestSocketPath::new();
    let first_listener = UnixListener::bind(&socket_path.0).unwrap();
    let first = SocketIdentity::read(&socket_path.0).unwrap();
    fs::remove_file(&socket_path.0).unwrap();
    let second_listener = UnixListener::bind(&socket_path.0).unwrap();
    let second = SocketIdentity::read(&socket_path.0).unwrap();

    assert_ne!(first, second);
    drop((first_listener, second_listener));
}

#[test]
fn persistent_connection_subscribes_caches_and_refreshes_after_event() {
    let socket_path = TestSocketPath::new();
    let listener = UnixListener::bind(&socket_path.0).unwrap();
    let (tree_sent_tx, tree_sent_rx) = mpsc::channel();
    let (send_event_tx, send_event_rx) = mpsc::channel();
    let (event_sent_tx, event_sent_rx) = mpsc::channel();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let subscribe = read_message(&mut stream);
        assert_eq!(subscribe.message_type, IPC_SUBSCRIBE);
        assert_eq!(subscribe.payload, EVENT_SUBSCRIPTIONS);
        write_message(&mut stream, IPC_SUBSCRIBE, br#"{"success":true}"#);

        let first_tree = read_message(&mut stream);
        assert_eq!(first_tree.message_type, IPC_GET_TREE);
        write_message(&mut stream, IPC_GET_TREE, br#"{"name":"first"}"#);
        tree_sent_tx.send(()).unwrap();

        send_event_rx.recv().unwrap();
        write_message(&mut stream, IPC_EVENT_MASK | 3, br#"{"change":"focus"}"#);
        event_sent_tx.send(()).unwrap();

        let second_tree = read_message(&mut stream);
        assert_eq!(second_tree.message_type, IPC_GET_TREE);
        write_message(&mut stream, IPC_GET_TREE, br#"{"name":"second"}"#);
    });

    let mut ipc = I3Ipc::connect(socket_path.0.clone()).unwrap();
    let first_generation = ipc.refresh_tree().unwrap();
    tree_sent_rx.recv().unwrap();
    assert_eq!(ipc.tree_payload().unwrap(), br#"{"name":"first"}"#);
    assert_eq!(ipc.refresh_tree().unwrap(), first_generation);

    send_event_tx.send(()).unwrap();
    event_sent_rx.recv().unwrap();
    let second_generation = ipc.refresh_tree().unwrap();
    assert_ne!(second_generation, first_generation);
    assert_eq!(ipc.tree_payload().unwrap(), br#"{"name":"second"}"#);
    server.join().unwrap();
}

fn read_message(stream: &mut UnixStream) -> IpcMessage {
    let mut header = [0_u8; IPC_HEADER_LEN];
    stream.read_exact(&mut header).unwrap();
    let payload_len = u32::from_ne_bytes(header[6..10].try_into().unwrap()) as usize;
    let mut payload = vec![0_u8; payload_len];
    stream.read_exact(&mut payload).unwrap();
    assert_eq!(&header[..6], IPC_MAGIC);
    IpcMessage {
        message_type: u32::from_ne_bytes(header[10..14].try_into().unwrap()),
        payload,
    }
}

fn write_message(stream: &mut UnixStream, message_type: u32, payload: &[u8]) {
    stream
        .write_all(&encode_frame(message_type, payload).unwrap())
        .unwrap();
}

struct TestSocketPath(PathBuf);

impl TestSocketPath {
    fn new() -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        Self(PathBuf::from(format!(
            "/tmp/codex-i3-ipc-{}-{unique}.sock",
            std::process::id()
        )))
    }
}

impl Drop for TestSocketPath {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
