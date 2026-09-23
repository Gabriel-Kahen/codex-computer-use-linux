use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{self, BufRead, Read};
use std::path::PathBuf;

pub const COSMIC_SERVICE_PROTOCOL_VERSION: u32 = 1;
pub const MAX_COSMIC_SERVICE_MESSAGE_BYTES: u64 = 1024 * 1024;

pub fn read_cosmic_service_message(reader: &mut impl BufRead) -> io::Result<Option<String>> {
    let mut line = String::new();
    let bytes_read = reader
        .take(MAX_COSMIC_SERVICE_MESSAGE_BYTES + 1)
        .read_line(&mut line)?;
    if bytes_read == 0 {
        return Ok(None);
    }
    if line.len() as u64 > MAX_COSMIC_SERVICE_MESSAGE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("COSMIC service message exceeded {MAX_COSMIC_SERVICE_MESSAGE_BYTES} bytes"),
        ));
    }
    if !line.ends_with('\n') {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "COSMIC service message was not newline-terminated",
        ));
    }
    Ok(Some(line))
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "name", rename_all = "kebab-case")]
pub enum CosmicServiceCommand {
    Probe,
    ListWindows,
    FocusedWindow,
    ActivateWindow {
        window_id: u64,
    },
    CaptureWindow {
        window_id: u64,
        output_path: PathBuf,
    },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CosmicServiceRequest {
    pub version: u32,
    pub id: u64,
    pub command: CosmicServiceCommand,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CosmicServiceResponse {
    pub version: u32,
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
            version: COSMIC_SERVICE_PROTOCOL_VERSION,
            id,
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(id: u64, error: impl Into<String>) -> Self {
        Self {
            version: COSMIC_SERVICE_PROTOCOL_VERSION,
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
