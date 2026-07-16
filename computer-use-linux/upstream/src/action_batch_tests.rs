use super::*;

fn batch(actions: Vec<BatchAction>) -> ActionBatchParams {
    ActionBatchParams {
        window_id: 42,
        actions,
    }
}

#[test]
fn accepts_common_click_type_and_submit_sequence() {
    let params = batch(vec![
        BatchAction::Click(BatchClick {
            element_index: Some(7),
            ..Default::default()
        }),
        BatchAction::TypeText {
            text: "hello".to_string(),
        },
        BatchAction::PressKey {
            key: "Enter".to_string(),
        },
    ]);

    assert_eq!(params.validate(), Ok(()));
}

#[test]
fn accepts_the_documented_tagged_json_shape() {
    let params: ActionBatchParams = serde_json::from_value(serde_json::json!({
        "window_id": 42,
        "actions": [
            {"type": "click", "x": 10, "y": 20, "relative": true},
            {"type": "type_text", "text": "hello"},
            {"type": "press_key", "key": "Ctrl+Enter"}
        ]
    }))
    .unwrap();

    assert_eq!(params.validate(), Ok(()));
    assert_eq!(params.actions.len(), 3);
}

#[test]
fn rejects_click_after_an_action() {
    let params = batch(vec![
        BatchAction::PressKey {
            key: "Tab".to_string(),
        },
        BatchAction::Click(BatchClick {
            x: Some(10),
            y: Some(20),
            ..Default::default()
        }),
    ]);

    assert_eq!(
        params.validate(),
        Err("A batch may contain one click, and it must be the first action.".to_string())
    );
}

#[test]
fn validates_every_action_before_execution() {
    let params = batch(vec![
        BatchAction::PressKey {
            key: "Tab".to_string(),
        },
        BatchAction::TypeText {
            text: "x".repeat(MAX_BATCH_TEXT_CHARS + 1),
        },
    ]);

    assert!(params.validate().unwrap_err().contains("text characters"));
}
