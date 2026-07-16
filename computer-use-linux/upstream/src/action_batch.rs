use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

pub(crate) const MAX_BATCH_ACTIONS: usize = 8;
pub(crate) const MAX_BATCH_TEXT_CHARS: usize = 4096;

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
pub(crate) struct ActionBatchParams {
    /// Exact window identifier inherited by every action in the batch.
    pub(crate) window_id: u64,
    pub(crate) actions: Vec<BatchAction>,
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum BatchAction {
    Click(BatchClick),
    TypeText { text: String },
    PressKey { key: String },
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
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

#[cfg(test)]
#[path = "action_batch_tests.rs"]
mod tests;
