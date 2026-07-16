use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::future::Future;

pub(crate) const MAX_BATCH_ACTIONS: usize = 8;
pub(crate) const MAX_BATCH_TEXT_CHARS: usize = 4096;
const MAX_BATCH_SELECTOR_CHARS: usize = 256;
const MAX_BATCH_STATES: usize = 16;
const MAX_BATCH_MESSAGE_BYTES: usize = 512;
pub(crate) const NON_EDITABLE_TEXT_LANDING_WARNING: &str = "the typed text likely went nowhere";
pub(crate) const NO_FOCUSED_ELEMENT_TEXT_LANDING_WARNING: &str =
    "the input may have landed nowhere";

#[derive(Debug, Clone, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct ActionBatchParams {
    /// Exact window identifier inherited by every action in the batch.
    pub(crate) window_id: u64,
    pub(crate) actions: Vec<BatchAction>,
}

#[derive(Debug, Clone, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum BatchAction {
    Click(BatchClick),
    TypeText { text: String },
    PressKey { key: String },
}

#[derive(Debug, Clone, Default, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct BatchClick {
    #[serde(default)]
    pub(crate) element_index: Option<u32>,
    #[serde(default)]
    pub(crate) role: Option<String>,
    #[serde(default)]
    pub(crate) name: Option<String>,
    #[serde(default)]
    pub(crate) text: Option<String>,
    #[serde(default)]
    pub(crate) states: Vec<String>,
    #[serde(default)]
    pub(crate) x: Option<i32>,
    #[serde(default)]
    pub(crate) y: Option<i32>,
    #[serde(default)]
    pub(crate) button: Option<String>,
    #[serde(default)]
    pub(crate) click_count: Option<u32>,
    /// Interpret coordinates relative to the inherited target window.
    #[serde(default)]
    pub(crate) relative: Option<bool>,
}

#[derive(Debug, Clone, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct ActionOutput {
    pub(crate) ok: bool,
    pub(crate) implemented: bool,
    pub(crate) action: String,
    pub(crate) message: String,
    // Kept in individual action responses, but always redacted from batch
    // results. `serde_json::Value` is omitted from the schema because strict
    // MCP clients reject its non-object schema.
    #[schemars(skip)]
    pub(crate) received: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct ActionBatchOutput {
    pub(crate) ok: bool,
    /// Number of actions that completed successfully.
    pub(crate) completed: usize,
    /// Zero-based index of the failed action, if execution began.
    pub(crate) failed_at: Option<usize>,
    /// Bounded results for attempted actions, including the failed action.
    pub(crate) results: Vec<ActionOutput>,
    pub(crate) error: Option<String>,
}

pub(crate) enum BatchActionRun {
    Completed(ActionOutput),
    TextLandingWarning(ActionOutput),
}

impl BatchActionRun {
    pub(crate) fn text(output: ActionOutput) -> Self {
        if output.message.contains(NON_EDITABLE_TEXT_LANDING_WARNING)
            || output
                .message
                .contains(NO_FOCUSED_ELEMENT_TEXT_LANDING_WARNING)
        {
            Self::TextLandingWarning(output)
        } else {
            Self::Completed(output)
        }
    }
}

impl ActionBatchOutput {
    pub(crate) fn validation_error(error: String) -> Self {
        Self {
            ok: false,
            completed: 0,
            failed_at: None,
            results: Vec::new(),
            error: Some(bounded_batch_message(&error)),
        }
    }
}

pub(crate) async fn execute_action_batch<F, Fut>(
    params: ActionBatchParams,
    mut run_action: F,
) -> ActionBatchOutput
where
    F: FnMut(BatchAction, u64) -> Fut,
    Fut: Future<Output = BatchActionRun>,
{
    let window_id = params.window_id;
    let mut results = Vec::with_capacity(params.actions.len());
    for (index, action) in params.actions.into_iter().enumerate() {
        let action_name = match &action {
            BatchAction::Click(_) => "click",
            BatchAction::TypeText { .. } => "type_text",
            BatchAction::PressKey { .. } => "press_key",
        };
        let (mut result, text_landing_warning) = match run_action(action, window_id).await {
            BatchActionRun::Completed(result) => (result, false),
            BatchActionRun::TextLandingWarning(result) => (result, true),
        };
        result.ok &= !text_landing_warning;
        result.action = action_name.to_string();
        result.message = bounded_batch_message(&result.message);
        result.received = None;
        let ok = result.ok;
        results.push(result);
        if !ok {
            let error = if text_landing_warning {
                format!(
                    "Action {index} reported that typed text may not have landed; later actions were not attempted."
                )
            } else {
                format!("Action {index} failed; later actions were not attempted.")
            };
            return ActionBatchOutput {
                ok: false,
                completed: index,
                failed_at: Some(index),
                results,
                error: Some(error),
            };
        }
    }

    ActionBatchOutput {
        ok: true,
        completed: results.len(),
        failed_at: None,
        results,
        error: None,
    }
}

fn bounded_batch_message(message: &str) -> String {
    const SUFFIX: &str = "... [truncated]";

    let truncated = message.len() > MAX_BATCH_MESSAGE_BYTES;
    let mut end = if truncated {
        MAX_BATCH_MESSAGE_BYTES - SUFFIX.len()
    } else {
        message.len()
    };
    while !message.is_char_boundary(end) {
        end -= 1;
    }
    let mut bounded: String = message[..end]
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .collect();
    if truncated {
        bounded.push_str(SUFFIX);
    }
    bounded
}

impl ActionBatchParams {
    pub(crate) fn validate(&self) -> Result<(), String> {
        if self.window_id == 0 {
            return Err("window_id must be a non-zero exact window identifier.".to_string());
        }
        if self.actions.is_empty() || self.actions.len() > MAX_BATCH_ACTIONS {
            return Err(format!(
                "actions must contain between 1 and {MAX_BATCH_ACTIONS} items."
            ));
        }

        let mut click_count = 0;
        let mut text_chars = 0;
        for (index, action) in self.actions.iter().enumerate() {
            match action {
                BatchAction::Click(click) => {
                    click_count += 1;
                    if click_count > 1 || index != 0 {
                        return Err(
                            "A batch may contain one click, and it must be the first action."
                                .to_string(),
                        );
                    }
                    click.validate()?;
                }
                BatchAction::TypeText { text } => {
                    if text.is_empty() {
                        return Err(format!("actions[{index}].text must not be empty."));
                    }
                    text_chars += text.chars().count();
                }
                BatchAction::PressKey { key } => {
                    if key.trim().is_empty() {
                        return Err(format!("actions[{index}].key must not be empty."));
                    }
                    validate_selector(key, "key")?;
                }
            }
        }
        if text_chars > MAX_BATCH_TEXT_CHARS {
            return Err(format!(
                "The batch contains {text_chars} text characters; the limit is {MAX_BATCH_TEXT_CHARS}."
            ));
        }
        Ok(())
    }
}

impl BatchClick {
    fn validate(&self) -> Result<(), String> {
        for (field, value) in [
            ("role", self.role.as_deref()),
            ("name", self.name.as_deref()),
            ("text", self.text.as_deref()),
            ("button", self.button.as_deref()),
        ] {
            if let Some(value) = value {
                validate_selector(value, field)?;
            }
        }
        if self.states.len() > MAX_BATCH_STATES {
            return Err(format!(
                "A click may contain at most {MAX_BATCH_STATES} states."
            ));
        }
        for state in &self.states {
            validate_selector(state, "state")?;
        }
        if self.x.is_some() != self.y.is_some() {
            return Err("A click must provide both x and y, or neither.".to_string());
        }
        let has_selector = self.element_index.is_some()
            || [&self.role, &self.name, &self.text]
                .into_iter()
                .flatten()
                .any(|value| !value.trim().is_empty());
        if self.x.is_none() && !has_selector {
            return Err(
                "A click requires x/y, element_index, or a non-empty semantic selector."
                    .to_string(),
            );
        }
        if self.relative == Some(true) && self.x.is_none() {
            return Err("A relative click requires x and y.".to_string());
        }
        if self
            .click_count
            .is_some_and(|click_count| !(1..=10).contains(&click_count))
        {
            return Err("click_count must be between 1 and 10.".to_string());
        }
        let unsupported_button = self.button.as_deref().filter(|button| {
            !matches!(
                button.to_ascii_lowercase().as_str(),
                "left" | "right" | "middle" | "side" | "extra" | "forward" | "back"
            )
        });
        if let Some(button) = unsupported_button {
            return Err(format!("Unsupported mouse button: {button}."));
        }
        Ok(())
    }
}

fn validate_selector(value: &str, field: &str) -> Result<(), String> {
    let characters = value.chars().count();
    if characters > MAX_BATCH_SELECTOR_CHARS {
        return Err(format!(
            "Batch {field} contains {characters} characters; the limit is {MAX_BATCH_SELECTOR_CHARS}."
        ));
    }
    Ok(())
}

#[cfg(test)]
#[path = "action_batch_tests.rs"]
mod tests;
