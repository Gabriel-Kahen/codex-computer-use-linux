pub(crate) use crate::coordination_protocol::MutationLane;
use fs2::FileExt;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_OWNER_LENGTH: usize = 128;
const MAX_CLAIM_TOKEN_LENGTH: usize = 128;

/// Credentials returned by the Hyprland companion's `claim_session_window` tool.
#[derive(Clone, Debug, Default, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct ClaimContext {
    #[serde(default, skip_serializing)]
    pub(crate) owner_thread_id: Option<String>,
    #[serde(default, skip_serializing)]
    pub(crate) claim_token: Option<String>,
}

pub(crate) struct ClaimGuard {
    _lock: File,
}

pub(crate) struct MutationGuards {
    _claim: ClaimGuard,
    _global_input: Option<File>,
}

#[derive(Clone)]
pub(crate) struct Coordinator {
    pub(crate) state_dir: PathBuf,
    pub(crate) binding: BTreeMap<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct ClaimState {
    version: u32,
    sessions: BTreeMap<String, ClaimSession>,
}

#[derive(Deserialize)]
struct ClaimSession {
    binding: BTreeMap<String, serde_json::Value>,
    claims: BTreeMap<String, Claim>,
}

#[derive(Deserialize)]
struct Claim {
    owner_thread_id: String,
    claim_token: String,
    expires_at: f64,
    #[serde(default)]
    inflight_until: Option<f64>,
    window: ClaimWindow,
}

#[derive(Deserialize)]
struct ClaimWindow {
    address: String,
}

impl Coordinator {
    pub(crate) fn from_env() -> Option<Self> {
        crate::diagnostics::hydrate_session_bus_env();
        let hyprland_instance = env::var("HYPRLAND_INSTANCE_SIGNATURE")
            .ok()
            .filter(|value| !value.trim().is_empty())?;
        let state_dir = env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))?
            .join("same-session-computer-use");
        let uid = fs::metadata("/proc/self").ok()?.uid();
        let runtime_dir =
            env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| format!("/run/user/{uid}"));
        let binding = BTreeMap::from([
            ("hyprland_instance".to_string(), hyprland_instance.into()),
            (
                "uid".to_string(),
                serde_json::Value::Number(serde_json::Number::from(uid)),
            ),
            ("wayland_display".to_string(), env_value("WAYLAND_DISPLAY")),
            ("xdg_runtime_dir".to_string(), runtime_dir.into()),
        ]);
        Some(Self { state_dir, binding })
    }

    fn acquire(
        &self,
        window_id: Option<u64>,
        context: &ClaimContext,
    ) -> Result<ClaimGuard, String> {
        self.prepare_state_dir()?;
        match window_id {
            Some(window_id) => self.acquire_window(window_id, context),
            None => self.acquire_unscoped(context),
        }
    }

    fn acquire_window(&self, window_id: u64, context: &ClaimContext) -> Result<ClaimGuard, String> {
        validate_context(context)?;
        let address = format!("0x{window_id:x}");
        let lock = open_lock(&self.window_lock_path(&format!("address:{address}")))?;
        FileExt::lock_exclusive(&lock)
            .map_err(|error| format!("failed to lock window claim: {error}"))?;

        let claims_lock = open_lock(&self.state_dir.join("window-claims.lock"))?;
        FileExt::lock_exclusive(&claims_lock)
            .map_err(|error| format!("failed to read window claims: {error}"))?;
        let mut claims = self.live_claims()?;
        let claim_key = claims
            .iter()
            .find_map(|(key, claim)| (claim.window.address == address).then(|| key.clone()));
        let claim = claim_key.and_then(|key| claims.remove(&key));
        FileExt::unlock(&claims_lock)
            .map_err(|error| format!("failed to unlock window claims: {error}"))?;
        if claim.is_none() && !claims.is_empty() {
            return Err(
                "target window must be claimed while this session has other active window claims"
                    .to_string(),
            );
        }
        authorize(claim.as_ref(), context)?;
        Ok(ClaimGuard { _lock: lock })
    }

    fn acquire_mutation(
        &self,
        window_id: Option<u64>,
        context: &ClaimContext,
        lane: MutationLane,
    ) -> Result<MutationGuards, String> {
        self.prepare_state_dir()?;
        let global_input = match lane {
            MutationLane::Window => None,
            MutationLane::PhysicalSeat => {
                let lock = open_lock(&self.state_dir.join("pointer-transaction.lock"))?;
                FileExt::lock_exclusive(&lock)
                    .map_err(|error| format!("failed to lock global input lane: {error}"))?;
                Some(lock)
            }
        };
        let claim = self.acquire(window_id, context)?;
        Ok(MutationGuards {
            _claim: claim,
            _global_input: global_input,
        })
    }

    fn acquire_unscoped(&self, context: &ClaimContext) -> Result<ClaimGuard, String> {
        validate_context(context)?;
        if context.owner_thread_id.is_some() || context.claim_token.is_some() {
            return Err("window_id is required when claim credentials are supplied".to_string());
        }
        let lock = open_lock(&self.state_dir.join("window-claims.lock"))?;
        FileExt::lock_exclusive(&lock)
            .map_err(|error| format!("failed to read window claims: {error}"))?;
        if !self.live_claims()?.is_empty() {
            return Err(
                "window_id is required while this session has active window claims".to_string(),
            );
        }
        Ok(ClaimGuard { _lock: lock })
    }

    fn prepare_state_dir(&self) -> Result<(), String> {
        fs::create_dir_all(self.state_dir.join("window-locks"))
            .map_err(|error| format!("failed to create claim lock directory: {error}"))?;
        fs::set_permissions(&self.state_dir, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("failed to secure claim state directory: {error}"))?;
        fs::set_permissions(
            self.state_dir.join("window-locks"),
            fs::Permissions::from_mode(0o700),
        )
        .map_err(|error| format!("failed to secure claim lock directory: {error}"))
    }

    pub(crate) fn binding_key(&self) -> String {
        digest(&serde_json::to_vec(&self.binding).expect("session binding must serialize"))
    }

    fn window_lock_path(&self, key: &str) -> PathBuf {
        let lock_key = digest(format!("{}\0{key}", self.binding_key()).as_bytes());
        self.state_dir
            .join("window-locks")
            .join(format!("{lock_key}.lock"))
    }

    fn live_claims(&self) -> Result<BTreeMap<String, Claim>, String> {
        Ok(self
            .active_session()?
            .map_or_else(BTreeMap::new, |session| {
                session
                    .claims
                    .into_iter()
                    .filter(|(_, claim)| claim_is_live(claim))
                    .collect()
            }))
    }

    fn active_session(&self) -> Result<Option<ClaimSession>, String> {
        let path = self.state_dir.join("window-claims.json");
        if !path.exists() {
            return Ok(None);
        }
        let state: ClaimState = serde_json::from_slice(
            &fs::read(path)
                .map_err(|error| format!("window claim state is unreadable: {error}"))?,
        )
        .map_err(|error| format!("window claim state is unreadable: {error}"))?;
        if state.version != 1 {
            return Err("window claim state has an unsupported format".to_string());
        }
        let session = state
            .sessions
            .into_iter()
            .find_map(|(key, session)| (key == self.binding_key()).then_some(session));
        if session
            .as_ref()
            .is_some_and(|session| session.binding != self.binding)
        {
            return Err(
                "window claim state does not match the active Hyprland session".to_string(),
            );
        }
        Ok(session)
    }
}

pub(crate) async fn acquire_mutation_guards(
    coordinator: Option<Coordinator>,
    window_id: Option<u64>,
    context: &ClaimContext,
    lane: MutationLane,
) -> Result<Option<MutationGuards>, String> {
    let Some(coordinator) = coordinator else {
        return Ok(None);
    };
    let context = context.clone();
    let guards = tokio::task::spawn_blocking(move || {
        coordinator.acquire_mutation(window_id, &context, lane)
    })
    .await
    .map_err(|error| format!("window claim check failed: {error}"))??;
    Ok(Some(guards))
}

fn env_value(name: &str) -> serde_json::Value {
    env::var(name)
        .ok()
        .map_or(serde_json::Value::Null, serde_json::Value::String)
}

fn open_lock(path: &Path) -> Result<File, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create claim lock directory: {error}"))?;
    }
    OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .map_err(|error| format!("failed to open claim lock: {error}"))
}

fn validate_context(context: &ClaimContext) -> Result<(), String> {
    if context
        .owner_thread_id
        .as_ref()
        .is_some_and(|owner| owner.is_empty() || owner.len() > MAX_OWNER_LENGTH)
    {
        return Err(format!(
            "owner_thread_id must contain 1..{MAX_OWNER_LENGTH} characters"
        ));
    }
    if context
        .claim_token
        .as_ref()
        .is_some_and(|token| token.is_empty() || token.len() > MAX_CLAIM_TOKEN_LENGTH)
    {
        return Err(format!(
            "claim_token must contain 1..{MAX_CLAIM_TOKEN_LENGTH} characters"
        ));
    }
    Ok(())
}

fn authorize(claim: Option<&Claim>, context: &ClaimContext) -> Result<(), String> {
    match claim {
        Some(claim)
            if context.owner_thread_id.as_deref() == Some(&claim.owner_thread_id)
                && context.claim_token.as_deref() == Some(&claim.claim_token) =>
        {
            Ok(())
        }
        Some(_) => Err("window is actively claimed by another computer-use agent".to_string()),
        None if context.claim_token.is_some() => {
            Err("claim_token is invalid, expired, or belongs to another window".to_string())
        }
        None => Ok(()),
    }
}

fn claim_is_live(claim: &Claim) -> bool {
    claim.inflight_until.unwrap_or(0.0).max(claim.expires_at) > now()
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
#[path = "claim_coordination_tests.rs"]
mod tests;
