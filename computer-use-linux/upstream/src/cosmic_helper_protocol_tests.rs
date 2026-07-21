use super::*;

#[test]
fn service_protocol_round_trips_activation_requests() {
    let request = CosmicServiceRequest {
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
        r#"{"id":9,"command":{"name":"activate-window","window_id":42}}"#
    );
}

#[test]
fn service_errors_do_not_fabricate_results() {
    let response = CosmicServiceResponse::error(3, "bad request");
    let value = serde_json::to_value(response).unwrap();

    assert_eq!(
        value,
        serde_json::json!({"id": 3, "ok": false, "error": "bad request"})
    );
}
