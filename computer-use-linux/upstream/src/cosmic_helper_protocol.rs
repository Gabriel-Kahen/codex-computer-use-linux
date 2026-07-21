use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "name", rename_all = "kebab-case")]
pub enum CosmicServiceCommand {
    Probe,
    ListWindows,
    FocusedWindow,
    ActivateWindow { window_id: u64 },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CosmicServiceRequest {
    pub id: u64,
    pub command: CosmicServiceCommand,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CosmicServiceResponse {
    pub id: u64,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl CosmicServiceResponse {
    pub fn success(id: u64, result: Value) -> Self {
        Self {
            id,
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(id: u64, error: impl Into<String>) -> Self {
        Self {
            id,
            ok: false,
            result: None,
            error: Some(error.into()),
        }
    }
}

#[cfg(test)]
#[path = "cosmic_helper_protocol_tests.rs"]
mod tests;
