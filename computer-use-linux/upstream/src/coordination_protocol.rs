//! Versioned, backend-neutral data contract for cross-process desktop coordination.
//!
//! The current coordinator still writes the Hyprland-specific version-1 journal.
//! These types freeze the version-2 wire contract before the coordinator and
//! desktop companions migrate to it in follow-up changes.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const PROTOCOL_VERSION: u32 = 2;
pub const MAX_SESSIONS: usize = 32;
pub const MAX_CLAIMS_PER_SESSION: usize = 128;
pub const MAX_OWNER_CHARS: usize = 200;
pub const MAX_OWNER_BYTES: usize = 512;
pub const MAX_TOKEN_CHARS: usize = 256;
pub const MAX_ID_CHARS: usize = 256;
pub const MAX_ATTRIBUTES: usize = 16;
pub const MAX_ATTRIBUTE_KEY_CHARS: usize = 64;
pub const MAX_ATTRIBUTE_VALUE_BYTES: usize = 512;
pub const MAX_SUMMARY_FIELDS: usize = 8;
pub const MAX_SUMMARY_VALUE_CHARS: usize = 256;
pub const MAX_SERIALIZED_STATE_BYTES: usize = 1_048_576;
pub const MIN_LEASE_SECONDS: u32 = 5;
pub const MAX_LEASE_SECONDS: u32 = 300;
pub const MAX_INFLIGHT_SECONDS: u32 = 300;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DesktopBackend {
    Cosmic,
    Gnome,
    Hyprland,
    I3,
    Niri,
    Plasma,
    X11,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MutationLane {
    Window,
    PhysicalSeat,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SessionIdentity {
    pub backend: DesktopBackend,
    pub uid: u32,
    pub attributes: BTreeMap<String, IdentityAttribute>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(untagged)]
pub enum IdentityAttribute {
    Text(String),
    Unsigned(u64),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub start_time: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WindowIdentity {
    pub backend: DesktopBackend,
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub process: Option<ProcessIdentity>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimWindow {
    pub identity: WindowIdentity,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub summary: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimRecord {
    pub owner_thread_id: String,
    pub claim_token: String,
    pub fencing_token: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner_process: Option<ProcessIdentity>,
    pub claimed_at_ms: u64,
    pub renewed_at_ms: u64,
    pub expires_at_ms: u64,
    pub lease_seconds: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inflight_until_ms: Option<u64>,
    pub window: ClaimWindow,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimSession {
    pub identity: SessionIdentity,
    pub next_fencing_token: u64,
    pub claims: BTreeMap<String, ClaimRecord>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimState {
    pub version: u32,
    pub sessions: BTreeMap<String, ClaimSession>,
}

impl SessionIdentity {
    pub fn key(&self) -> String {
        digest(&canonical_json(self))
    }
}

impl WindowIdentity {
    pub fn key(&self, session: &SessionIdentity) -> String {
        #[derive(Serialize)]
        struct LockIdentity<'a> {
            session: &'a SessionIdentity,
            window: &'a WindowIdentity,
        }

        digest(&canonical_json(&LockIdentity {
            session,
            window: self,
        }))
    }
}

impl ClaimState {
    pub fn validate(&self) -> Result<(), String> {
        if self.version != PROTOCOL_VERSION {
            return Err(format!(
                "coordination protocol version must be {PROTOCOL_VERSION}"
            ));
        }
        if self.sessions.len() > MAX_SESSIONS {
            return Err(format!(
                "coordination state may contain at most {MAX_SESSIONS} sessions"
            ));
        }
        let serialized = serde_json::to_vec(self)
            .map_err(|error| format!("coordination state is not serializable: {error}"))?;
        if serialized.len() > MAX_SERIALIZED_STATE_BYTES {
            return Err(format!(
                "coordination state exceeds {MAX_SERIALIZED_STATE_BYTES} bytes"
            ));
        }

        for (session_key, session) in &self.sessions {
            validate_session(session)?;
            if session_key != &session.identity.key() {
                return Err("coordination session key does not match its identity".to_string());
            }
            if session.claims.len() > MAX_CLAIMS_PER_SESSION {
                return Err(format!(
                    "coordination session may contain at most {MAX_CLAIMS_PER_SESSION} claims"
                ));
            }
            let mut fencing_tokens = std::collections::BTreeSet::new();
            for (window_key, claim) in &session.claims {
                validate_claim(&session.identity, window_key, claim)?;
                if !fencing_tokens.insert(claim.fencing_token) {
                    return Err(
                        "coordination session contains a duplicate fencing token".to_string()
                    );
                }
            }
            let highest_fencing_token = fencing_tokens.last().copied().unwrap_or(0);
            if session.next_fencing_token == 0
                || session.next_fencing_token <= highest_fencing_token
            {
                return Err(
                    "next_fencing_token must be greater than every issued token".to_string()
                );
            }
        }
        Ok(())
    }
}

fn validate_session(session: &ClaimSession) -> Result<(), String> {
    if session.identity.attributes.is_empty() || session.identity.attributes.len() > MAX_ATTRIBUTES
    {
        return Err(format!(
            "session identity must contain 1..{MAX_ATTRIBUTES} attributes"
        ));
    }
    for (key, value) in &session.identity.attributes {
        if key.is_empty()
            || key.chars().count() > MAX_ATTRIBUTE_KEY_CHARS
            || key.chars().any(char::is_control)
        {
            return Err("session identity contains an invalid attribute name".to_string());
        }
        if let IdentityAttribute::Text(value) = value {
            validate_text(
                "session identity attribute",
                value,
                MAX_ATTRIBUTE_VALUE_BYTES,
                MAX_ATTRIBUTE_VALUE_BYTES,
            )?;
        }
    }
    Ok(())
}

fn validate_claim(
    session: &SessionIdentity,
    window_key: &str,
    claim: &ClaimRecord,
) -> Result<(), String> {
    validate_text(
        "owner_thread_id",
        &claim.owner_thread_id,
        MAX_OWNER_CHARS,
        MAX_OWNER_BYTES,
    )?;
    if claim.fencing_token == 0 {
        return Err("fencing_token must be nonzero".to_string());
    }
    validate_text(
        "claim_token",
        &claim.claim_token,
        MAX_TOKEN_CHARS,
        MAX_TOKEN_CHARS,
    )?;
    validate_text(
        "window id",
        &claim.window.identity.id,
        MAX_ID_CHARS,
        MAX_ID_CHARS * 4,
    )?;
    if claim.window.identity.backend != session.backend {
        return Err("window and session backends do not match".to_string());
    }
    if window_key != claim.window.identity.key(session) {
        return Err("coordination window key does not match its identity".to_string());
    }
    if !(MIN_LEASE_SECONDS..=MAX_LEASE_SECONDS).contains(&claim.lease_seconds) {
        return Err(format!(
            "lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        ));
    }
    let expected_expiry = claim
        .renewed_at_ms
        .checked_add(u64::from(claim.lease_seconds) * 1000);
    let latest_inflight = claim
        .expires_at_ms
        .checked_add(u64::from(MAX_INFLIGHT_SECONDS) * 1000);
    if claim.claimed_at_ms == 0
        || claim.renewed_at_ms < claim.claimed_at_ms
        || expected_expiry != Some(claim.expires_at_ms)
        || claim.inflight_until_ms.is_some_and(|deadline| {
            deadline < claim.renewed_at_ms || Some(deadline) > latest_inflight
        })
    {
        return Err("claim contains an invalid deadline".to_string());
    }
    if claim
        .owner_process
        .as_ref()
        .is_some_and(|process| process.pid == 0 || process.start_time == 0)
        || claim
            .window
            .identity
            .process
            .as_ref()
            .is_some_and(|process| process.pid == 0 || process.start_time == 0)
    {
        return Err("claim contains an invalid process identity".to_string());
    }
    if claim.window.summary.len() > MAX_SUMMARY_FIELDS
        || claim.window.summary.iter().any(|(key, value)| {
            key.is_empty()
                || key.chars().count() > MAX_ATTRIBUTE_KEY_CHARS
                || key.chars().any(char::is_control)
                || value.chars().count() > MAX_SUMMARY_VALUE_CHARS
                || value.chars().any(char::is_control)
        })
    {
        return Err("window summary exceeds its bounds".to_string());
    }
    Ok(())
}

fn validate_text(
    name: &str,
    value: &str,
    max_chars: usize,
    max_bytes: usize,
) -> Result<(), String> {
    if value.is_empty()
        || value.chars().count() > max_chars
        || value.len() > max_bytes
        || value.chars().any(char::is_control)
    {
        return Err(format!("{name} exceeds its size limit"));
    }
    Ok(())
}

fn canonical_json(value: &impl Serialize) -> Vec<u8> {
    let value = serde_json::to_value(value).expect("coordination identity must serialize");
    serde_json::to_vec(&CanonicalValue::from(value))
        .expect("canonical coordination identity must serialize")
}

fn digest(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}

#[derive(Serialize)]
#[serde(untagged)]
enum CanonicalValue {
    Null,
    Bool(bool),
    Number(serde_json::Number),
    String(String),
    Array(Vec<CanonicalValue>),
    Object(BTreeMap<String, CanonicalValue>),
}

impl From<serde_json::Value> for CanonicalValue {
    fn from(value: serde_json::Value) -> Self {
        match value {
            serde_json::Value::Null => Self::Null,
            serde_json::Value::Bool(value) => Self::Bool(value),
            serde_json::Value::Number(value) => Self::Number(value),
            serde_json::Value::String(value) => Self::String(value),
            serde_json::Value::Array(values) => {
                Self::Array(values.into_iter().map(Self::from).collect())
            }
            serde_json::Value::Object(values) => Self::Object(
                values
                    .into_iter()
                    .map(|(key, value)| (key, Self::from(value)))
                    .collect(),
            ),
        }
    }
}

#[cfg(test)]
#[path = "coordination_protocol_tests.rs"]
mod tests;
