use super::*;
use std::os::unix::net::UnixListener;
use std::sync::mpsc;
use std::time::{SystemTime, UNIX_EPOCH};

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = PathBuf::from("/tmp").join(format!("cu-niri-{}-{unique}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn window(id: u64, focused: bool, width: i64) -> String {
    format!(
        r#"{{"id":{id},"title":"Window {id}","app_id":"app-{id}","pid":{id},"workspace_id":1,"is_focused":{focused},"layout":{{"window_size":[{width},600]}}}}"#
    )
}

#[test]
fn applies_initial_and_incremental_window_events() {
    let mut state = EventState::default();
    state
        .apply_line(&format!(
            r#"{{"WindowsChanged":{{"windows":[{}]}}}}"#,
            window(1, true, 800)
        ))
        .unwrap();
    state
        .apply_line(&format!(
            r#"{{"WindowOpenedOrChanged":{{"window":{}}}}}"#,
            window(2, false, 900)
        ))
        .unwrap();
    state
        .apply_line(r#"{"WindowFocusChanged":{"id":2}}"#)
        .unwrap();
    state
        .apply_line(r#"{"WindowLayoutsChanged":{"changes":[[2,{"window_size":[1000,700]}]]}}"#)
        .unwrap();
    state.apply_line(r#"{"WindowClosed":{"id":1}}"#).unwrap();

    assert!(state.ready);
    assert_eq!(state.windows.len(), 1);
    let remaining = state.windows.get(&2).unwrap();
    assert!(remaining.is_focused);
    assert_eq!(
        remaining.layout.as_ref().unwrap().window_size,
        Some([1000, 700])
    );
}

#[test]
fn ignores_unknown_valid_events_but_rejects_malformed_json() {
    let mut state = EventState::default();
    assert!(!state
        .apply_line(r#"{"FutureNiriEvent":{"new_field":true}}"#)
        .unwrap());
    assert!(state.apply_line("not json").is_err());
}

#[test]
fn reconnects_after_socket_replacement_and_replaces_stale_state() {
    let temporary = TestDir::new();
    let socket_path = temporary.path().join("niri.sock");
    let first_listener = UnixListener::bind(&socket_path).unwrap();
    let (first_connected_tx, first_connected_rx) = mpsc::channel();
    let (release_first_tx, release_first_rx) = mpsc::channel();
    let first_server = thread::spawn(move || {
        let (stream, _) = first_listener.accept().unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut request = String::new();
        reader.read_line(&mut request).unwrap();
        assert_eq!(request.trim(), r#""EventStream""#);
        let mut writer = stream;
        writeln!(writer, r#"{{"Ok":"Handled"}}"#).unwrap();
        writeln!(
            writer,
            r#"{{"WindowsChanged":{{"windows":[{}]}}}}"#,
            window(1, true, 800)
        )
        .unwrap();
        writer.flush().unwrap();
        first_connected_tx.send(()).unwrap();
        release_first_rx
            .recv_timeout(Duration::from_secs(2))
            .unwrap();
    });

    let client = EventClient::start(socket_path.clone());
    first_connected_rx
        .recv_timeout(Duration::from_secs(1))
        .unwrap();
    assert_eq!(client.snapshot(Duration::from_secs(1)).unwrap()[0].id, 1);

    fs::remove_file(&socket_path).unwrap();
    let second_listener = UnixListener::bind(&socket_path).unwrap();
    let second_server = thread::spawn(move || {
        let (stream, _) = second_listener.accept().unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut request = String::new();
        reader.read_line(&mut request).unwrap();
        let mut writer = stream;
        writeln!(writer, r#"{{"Ok":"Handled"}}"#).unwrap();
        writeln!(
            writer,
            r#"{{"WindowsChanged":{{"windows":[{}]}}}}"#,
            window(2, true, 900)
        )
        .unwrap();
        writer.flush().unwrap();
    });

    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if client
            .snapshot(Duration::from_millis(100))
            .ok()
            .is_some_and(|windows| windows.first().is_some_and(|window| window.id == 2))
        {
            break;
        }
        assert!(Instant::now() < deadline, "client did not reconnect");
    }
    client.stop();
    release_first_tx.send(()).unwrap();
    first_server.join().unwrap();
    second_server.join().unwrap();
}

#[test]
fn accepts_only_successful_handled_replies() {
    assert!(ensure_handled_reply(r#"{"Ok":"Handled"}"#).is_ok());
    assert!(ensure_handled_reply(r#"{"Err":"unsupported"}"#)
        .unwrap_err()
        .to_string()
        .contains("unsupported"));
}
