use super::*;
use std::io::Cursor;

#[test]
fn service_protocol_round_trips_activation_requests() {
    let request = CosmicServiceRequest {
        version: COSMIC_SERVICE_PROTOCOL_VERSION,
        id: 9,
        command: CosmicServiceCommand::ActivateWindow { window_id: 42 },
    };
    let json = serde_json::to_string(&request).unwrap();

    assert_eq!(
        serde_json::from_str::<CosmicServiceRequest>(&json).unwrap(),
        request
    );
    assert_eq!(
        json,
        r#"{"version":1,"id":9,"command":{"name":"activate-window","window_id":42}}"#
    );
}

#[test]
fn service_errors_do_not_fabricate_results() {
    let response = CosmicServiceResponse::error(3, "bad request");
    let value = serde_json::to_value(response).unwrap();

    assert_eq!(
        value,
        serde_json::json!({"version": 1, "id": 3, "ok": false, "error": "bad request"})
    );
}

#[test]
fn service_message_reader_rejects_oversized_messages() {
    let mut message = vec![b'x'; MAX_COSMIC_SERVICE_MESSAGE_BYTES as usize + 1];
    message.push(b'\n');

    let error = read_cosmic_service_message(&mut Cursor::new(message)).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
}

#[test]
fn service_message_reader_requires_newline_termination() {
    let error = read_cosmic_service_message(&mut Cursor::new(b"{}".to_vec())).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::UnexpectedEof);
}
