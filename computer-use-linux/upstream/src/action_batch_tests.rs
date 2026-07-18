use super::*;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::future::ready;

fn batch(actions: Vec<BatchAction>) -> ActionBatchParams {
    ActionBatchParams {
        window_id: 42,
        actions,
    }
}

fn action_output(ok: bool, message: impl Into<String>) -> ActionOutput {
    ActionOutput {
        ok,
        implemented: true,
        action: "untrusted".to_string(),
        message: message.into(),
        received: Some(serde_json::json!({"unbounded": "argument"})),
    }
}

fn bounded_output(action: &str, ok: bool, message: &str) -> ActionOutput {
    ActionOutput {
        ok,
        implemented: true,
        action: action.to_string(),
        message: message.to_string(),
        received: None,
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

#[test]
fn rejects_unbounded_semantic_selectors() {
    let params = batch(vec![BatchAction::Click(BatchClick {
        name: Some("x".repeat(MAX_BATCH_SELECTOR_CHARS + 1)),
        ..Default::default()
    })]);

    assert_eq!(
        params.validate(),
        Err(format!(
            "Batch name contains {} characters; the limit is {MAX_BATCH_SELECTOR_CHARS}.",
            MAX_BATCH_SELECTOR_CHARS + 1
        ))
    );
}

#[tokio::test]
async fn executes_actions_in_order_with_the_exact_inherited_window_id() {
    let actions = vec![
        BatchAction::PressKey {
            key: "Tab".to_string(),
        },
        BatchAction::TypeText {
            text: "hello".to_string(),
        },
        BatchAction::PressKey {
            key: "Enter".to_string(),
        },
    ];
    let calls = RefCell::new(Vec::new());
    let outputs = RefCell::new(VecDeque::from([
        action_output(true, "first"),
        action_output(true, "second"),
        action_output(true, "third"),
    ]));

    let result = execute_action_batch(batch(actions.clone()), |action, window_id| {
        calls.borrow_mut().push((window_id, action));
        ready(BatchActionRun::Completed(
            outputs.borrow_mut().pop_front().unwrap(),
        ))
    })
    .await;

    assert_eq!(
        calls.into_inner(),
        actions
            .into_iter()
            .map(|action| (42, action))
            .collect::<Vec<_>>()
    );
    assert_eq!(
        result,
        ActionBatchOutput {
            ok: true,
            completed: 3,
            failed_at: None,
            results: vec![
                bounded_output("press_key", true, "first"),
                bounded_output("type_text", true, "second"),
                bounded_output("press_key", true, "third"),
            ],
            error: None,
        }
    );
}

#[tokio::test]
async fn stops_after_a_middle_failure_and_reports_completed_actions() {
    let actions = vec![
        BatchAction::PressKey {
            key: "Tab".to_string(),
        },
        BatchAction::TypeText {
            text: "hello".to_string(),
        },
        BatchAction::PressKey {
            key: "Enter".to_string(),
        },
    ];
    let calls = RefCell::new(Vec::new());
    let outputs = RefCell::new(VecDeque::from([
        action_output(true, "first"),
        action_output(false, "failed"),
        action_output(true, "must not run"),
    ]));

    let result = execute_action_batch(batch(actions.clone()), |action, window_id| {
        calls.borrow_mut().push((window_id, action));
        ready(BatchActionRun::Completed(
            outputs.borrow_mut().pop_front().unwrap(),
        ))
    })
    .await;

    assert_eq!(
        calls.into_inner(),
        vec![(42, actions[0].clone()), (42, actions[1].clone())]
    );
    assert_eq!(outputs.into_inner().len(), 1);
    assert_eq!(
        result,
        ActionBatchOutput {
            ok: false,
            completed: 1,
            failed_at: Some(1),
            results: vec![
                bounded_output("press_key", true, "first"),
                bounded_output("type_text", false, "failed"),
            ],
            error: Some("Action 1 failed; later actions were not attempted.".to_string()),
        }
    );
}

#[tokio::test]
async fn stops_before_enter_when_text_landing_feedback_warns() {
    let actions = vec![
        BatchAction::TypeText {
            text: "hello".to_string(),
        },
        BatchAction::PressKey {
            key: "Enter".to_string(),
        },
    ];
    let calls = RefCell::new(Vec::new());
    let warning = format!(
        "WARNING: focused element is button, which is not editable — {NON_EDITABLE_TEXT_LANDING_WARNING}."
    );
    let outputs = RefCell::new(VecDeque::from([
        action_output(true, warning.clone()),
        action_output(true, "must not run"),
    ]));

    let result = execute_action_batch(batch(actions.clone()), |action, window_id| {
        calls.borrow_mut().push((window_id, action));
        ready(BatchActionRun::text(
            outputs.borrow_mut().pop_front().unwrap(),
        ))
    })
    .await;

    assert_eq!(calls.into_inner(), vec![(42, actions[0].clone())]);
    assert_eq!(outputs.into_inner().len(), 1);
    assert_eq!(
        result,
        ActionBatchOutput {
            ok: false,
            completed: 0,
            failed_at: Some(0),
            results: vec![bounded_output("type_text", false, &warning)],
            error: Some(
                "Action 0 reported that typed text may not have landed; later actions were not attempted."
                    .to_string()
            ),
        }
    );
}

#[tokio::test]
async fn redacts_arguments_and_bounds_the_serialized_batch_output() {
    let actions = (0..MAX_BATCH_ACTIONS)
        .map(|_| BatchAction::PressKey {
            key: "Tab".to_string(),
        })
        .collect();
    let outputs = RefCell::new(VecDeque::from(
        (0..MAX_BATCH_ACTIONS)
            .map(|_| {
                let mut output = action_output(true, format!("\0\"{}", "🦀".repeat(4096)));
                output.received = Some(serde_json::json!({"text": "x".repeat(100_000)}));
                BatchActionRun::Completed(output)
            })
            .collect::<Vec<_>>(),
    ));

    let result = execute_action_batch(batch(actions), |_, _| {
        ready(outputs.borrow_mut().pop_front().unwrap())
    })
    .await;
    let serialized = serde_json::to_string(&result).unwrap();

    assert!(result.results.iter().all(|result| result.received.is_none()
        && result.message.len() <= MAX_ACTION_RESULT_MESSAGE_BYTES));
    assert!(
        serialized.len() < 10_000,
        "serialized length: {}",
        serialized.len()
    );
    assert!(!serialized.contains(&"x".repeat(1000)));

    let validation = ActionBatchOutput::validation_error("\0\"".repeat(10_000));
    assert!(serde_json::to_string(&validation).unwrap().len() < 2_000);
}
