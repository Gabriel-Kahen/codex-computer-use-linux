use crate::coordination_identity::{self, CoordinationScope};
pub(crate) use crate::coordination_protocol::MutationLane;
use crate::coordination_protocol::{
    ClaimRecord, ClaimState as ProtocolClaimState, SessionIdentity, WindowIdentity,
    MAX_OWNER_BYTES, MAX_OWNER_CHARS, MAX_SERIALIZED_STATE_BYTES, MAX_TOKEN_CHARS,
    PROTOCOL_VERSION,
};
use fs2::FileExt;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// Credentials returned by the Hyprland companion's `claim_session_window` tool.
#[derive(Clone, Debug, Default, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
pub(crate) struct ClaimContext {
    #[serde(default, skip_serializing)]
    pub(crate) owner_thread_id: Option<String>,
    #[serde(default, skip_serializing)]
    pub(crate) claim_token: Option<String>,
}

pub(crate) struct ClaimGuard {
    _locks: Vec<File>,
}

pub(crate) struct MutationGuards {
    _claim: Option<ClaimGuard>,
    _global_input: Option<File>,
    _journal: Option<File>,
}

#[derive(Clone)]
pub(crate) struct Coordinator {
    pub(crate) state_dir: PathBuf,
    pub(crate) binding: BTreeMap<String, serde_json::Value>,
    pub(crate) session: Option<SessionIdentity>,
    pub(crate) window: Option<WindowIdentity>,
}

#[derive(Deserialize)]
struct LegacyClaimState {
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

struct LiveClaim {
    owner_thread_id: String,
    claim_token: String,
    legacy_address: Option<String>,
    window: Option<WindowIdentity>,
}

impl LiveClaim {
    fn matches(&self, address: &str, window: Option<&WindowIdentity>) -> bool {
        self.legacy_address.as_deref() == Some(address)
            || self
                .window
                .as_ref()
                .zip(window)
                .is_some_and(|(claimed, target)| claimed == target)
    }
}

impl From<Claim> for LiveClaim {
    fn from(claim: Claim) -> Self {
        Self {
            owner_thread_id: claim.owner_thread_id,
            claim_token: claim.claim_token,
            legacy_address: Some(claim.window.address),
            window: None,
        }
    }
}

impl From<ClaimRecord> for LiveClaim {
    fn from(claim: ClaimRecord) -> Self {
        Self {
            owner_thread_id: claim.owner_thread_id,
            claim_token: claim.claim_token,
            legacy_address: None,
            window: Some(claim.window.identity),
        }
    }
}

impl Coordinator {
    fn from_scope(scope: CoordinationScope) -> Self {
        Self {
            state_dir: scope.state_dir,
            binding: scope.legacy_hyprland_binding.unwrap_or_default(),
            session: Some(scope.session),
            window: scope.window,
        }
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
        let mut lock_paths = Vec::new();
        if !self.binding.is_empty() {
            lock_paths.push(self.window_lock_path(&format!("address:{address}")));
        }
        if let (Some(session), Some(window)) = (&self.session, &self.window) {
            lock_paths.push(
                self.state_dir
                    .join("window-locks")
                    .join(format!("{}.lock", window.key(session))),
            );
        }
        if lock_paths.is_empty() {
            return Err("cannot identify the target window for coordination".to_string());
        }
        lock_paths.sort();
        lock_paths.dedup();
        let mut locks = Vec::with_capacity(lock_paths.len());
        for path in lock_paths {
            let lock = open_lock(&path)?;
            FileExt::lock_exclusive(&lock)
                .map_err(|error| format!("failed to lock window claim: {error}"))?;
            locks.push(lock);
        }

        let claims_lock = open_lock(&self.state_dir.join("window-claims.lock"))?;
        FileExt::lock_exclusive(&claims_lock)
            .map_err(|error| format!("failed to read window claims: {error}"))?;
        let mut claims = self.live_claims()?;
        let claim_key = claims
            .iter()
            .position(|claim| claim.matches(&address, self.window.as_ref()));
        let claim = claim_key.map(|index| claims.remove(index));
        FileExt::unlock(&claims_lock)
            .map_err(|error| format!("failed to unlock window claims: {error}"))?;
        if claim.is_none() && !claims.is_empty() {
            return Err(
                "target window must be claimed while this session has other active window claims"
                    .to_string(),
            );
        }
        authorize(claim.as_ref(), context)?;
        Ok(ClaimGuard { _locks: locks })
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
            _claim: Some(claim),
            _global_input: global_input,
            _journal: None,
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
        Ok(ClaimGuard { _locks: vec![lock] })
    }

    fn prepare_state_dir(&self) -> Result<(), String> {
        prepare_state_dir(&self.state_dir)
    }

    pub(crate) fn binding_key(&self) -> String {
        digest(&serde_json::to_vec(&self.binding).expect("session binding must serialize"))
    }

    fn window_lock_path(&self, key: &str) -> PathBuf {
        legacy_window_lock_path(&self.state_dir, &self.binding, key)
    }

    fn live_claims(&self) -> Result<Vec<LiveClaim>, String> {
        let Some(bytes) = read_claim_state(&self.state_dir.join("window-claims.json"))? else {
            return Ok(Vec::new());
        };
        let version = serde_json::from_slice::<serde_json::Value>(&bytes)
            .ok()
            .and_then(|value| value.get("version")?.as_u64())
            .ok_or("window claim state has an unsupported format")?;
        match version {
            1 => self.live_legacy_claims(&bytes),
            value if value == u64::from(PROTOCOL_VERSION) => self.live_protocol_claims(&bytes),
            _ => Err("window claim state has an unsupported format".to_string()),
        }
    }

    fn live_legacy_claims(&self, bytes: &[u8]) -> Result<Vec<LiveClaim>, String> {
        if self.binding.is_empty() {
            return Err("version-1 claim state is only valid for Hyprland".to_string());
        }
        let state: LegacyClaimState = serde_json::from_slice(bytes)
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
        Ok(session.map_or_else(Vec::new, |session| {
            session
                .claims
                .into_values()
                .filter(claim_is_live)
                .map(LiveClaim::from)
                .collect()
        }))
    }

    fn live_protocol_claims(&self, bytes: &[u8]) -> Result<Vec<LiveClaim>, String> {
        let state: ProtocolClaimState = serde_json::from_slice(bytes)
            .map_err(|error| format!("window claim state is unreadable: {error}"))?;
        state.validate()?;
        let session = self
            .session
            .as_ref()
            .ok_or("cannot enforce version-2 claims without a session identity")?;
        Ok(state
            .sessions
            .get(&session.key())
            .filter(|entry| &entry.identity == session)
            .map_or_else(Vec::new, |entry| {
                entry
                    .claims
                    .values()
                    .filter(|claim| protocol_claim_is_live(claim))
                    .cloned()
                    .map(LiveClaim::from)
                    .collect()
            }))
    }
}

pub(crate) fn legacy_window_lock_path(
    state_dir: &Path,
    binding: &BTreeMap<String, serde_json::Value>,
    key: &str,
) -> PathBuf {
    let binding_key = digest(&serde_json::to_vec(binding).expect("binding must serialize"));
    let lock_key = digest(format!("{binding_key}\0{key}").as_bytes());
    state_dir
        .join("window-locks")
        .join(format!("{lock_key}.lock"))
}

pub(crate) fn prepare_state_dir(state_dir: &Path) -> Result<(), String> {
    if !state_dir.exists() {
        fs::create_dir_all(state_dir)
            .map_err(|error| format!("failed to create claim state directory: {error}"))?;
    }
    validate_private_directory(state_dir, "claim state directory")?;
    fs::set_permissions(state_dir, fs::Permissions::from_mode(0o700))
        .map_err(|error| format!("failed to secure claim state directory: {error}"))?;
    let lock_dir = state_dir.join("window-locks");
    fs::create_dir_all(&lock_dir)
        .map_err(|error| format!("failed to create claim lock directory: {error}"))?;
    validate_private_directory(&lock_dir, "claim lock directory")?;
    fs::set_permissions(lock_dir, fs::Permissions::from_mode(0o700))
        .map_err(|error| format!("failed to secure claim lock directory: {error}"))
}

pub(crate) async fn acquire_mutation_guards(
    window_id: Option<u64>,
    context: &ClaimContext,
    lane: MutationLane,
) -> Result<Option<MutationGuards>, String> {
    let coordinator = match coordination_identity::resolve(window_id).await {
        Ok(scope) => Coordinator::from_scope(scope),
        #[cfg(not(target_os = "linux"))]
        Err(_) => return Ok(None),
        #[cfg(target_os = "linux")]
        Err(error) => {
            let state_dir = coordination_identity::state_dir()
                .ok_or("cannot locate the coordination state directory")?;
            if context.owner_thread_id.is_some() || context.claim_token.is_some() {
                return Err(format!("window claim check failed closed: {error}"));
            }
            let guards = tokio::task::spawn_blocking(move || {
                let coordinator = Coordinator {
                    state_dir,
                    binding: BTreeMap::new(),
                    session: None,
                    window: None,
                };
                coordinator.prepare_state_dir()?;
                let global_input = if lane == MutationLane::PhysicalSeat {
                    let lock = open_lock(&coordinator.state_dir.join("pointer-transaction.lock"))?;
                    FileExt::lock_exclusive(&lock)
                        .map_err(|error| format!("failed to lock global input lane: {error}"))?;
                    Some(lock)
                } else {
                    None
                };
                let journal = open_lock(&coordinator.state_dir.join("window-claims.lock"))?;
                FileExt::lock_exclusive(&journal)
                    .map_err(|io_error| format!("failed to lock window claims: {io_error}"))?;
                match fs::symlink_metadata(coordinator.state_dir.join("window-claims.json")) {
                    Err(io_error) if io_error.kind() == std::io::ErrorKind::NotFound => {}
                    _ => return Err(format!("window claim check failed closed: {error}")),
                }
                Ok::<_, String>(MutationGuards {
                    _claim: None,
                    _global_input: global_input,
                    _journal: Some(journal),
                })
            })
            .await
            .map_err(|join_error| format!("coordination fallback failed: {join_error}"))??;
            return Ok(Some(guards));
        }
    };
    acquire_mutation_guards_with(coordinator, window_id, context, lane).await
}

pub(crate) async fn acquire_mutation_guards_with(
    coordinator: Coordinator,
    window_id: Option<u64>,
    context: &ClaimContext,
    lane: MutationLane,
) -> Result<Option<MutationGuards>, String> {
    let context = context.clone();
    let guards = tokio::task::spawn_blocking(move || {
        coordinator.acquire_mutation(window_id, &context, lane)
    })
    .await
    .map_err(|error| format!("window claim check failed: {error}"))??;
    Ok(Some(guards))
}

pub(crate) fn open_lock(path: &Path) -> Result<File, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create claim lock directory: {error}"))?;
    }
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| format!("failed to open claim lock: {error}"))?;
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("failed to secure claim lock: {error}"))?;
    validate_private_file(
        &file
            .metadata()
            .map_err(|error| format!("failed to inspect claim lock: {error}"))?,
        "claim lock",
    )?;
    Ok(file)
}

pub(crate) fn validate_context(context: &ClaimContext) -> Result<(), String> {
    if context.owner_thread_id.as_ref().is_some_and(|owner| {
        owner.is_empty()
            || owner.chars().count() > MAX_OWNER_CHARS
            || owner.len() > MAX_OWNER_BYTES
            || owner.chars().any(char::is_control)
    }) {
        return Err(format!(
            "owner_thread_id must contain 1..{MAX_OWNER_CHARS} characters"
        ));
    }
    if context.claim_token.as_ref().is_some_and(|token| {
        token.is_empty()
            || token.chars().count() > MAX_TOKEN_CHARS
            || token.len() > MAX_TOKEN_CHARS
            || token.chars().any(char::is_control)
    }) {
        return Err(format!(
            "claim_token must contain 1..{MAX_TOKEN_CHARS} characters"
        ));
    }
    Ok(())
}

fn authorize(claim: Option<&LiveClaim>, context: &ClaimContext) -> Result<(), String> {
    match claim {
        Some(claim)
            if context.owner_thread_id.as_deref() == Some(&claim.owner_thread_id)
                && context
                    .claim_token
                    .as_deref()
                    .is_some_and(|token| constant_time_eq(token, &claim.claim_token)) =>
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

pub(crate) fn constant_time_eq(left: &str, right: &str) -> bool {
    let mut difference = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        difference |= usize::from(
            left.as_bytes().get(index).copied().unwrap_or(0)
                ^ right.as_bytes().get(index).copied().unwrap_or(0),
        );
    }
    difference == 0
}

fn claim_is_live(claim: &Claim) -> bool {
    claim.inflight_until.unwrap_or(0.0).max(claim.expires_at) > now()
}

pub(crate) fn legacy_state_has_live_claims(bytes: &[u8]) -> Result<bool, String> {
    let state: LegacyClaimState = serde_json::from_slice(bytes)
        .map_err(|error| format!("legacy window claim state is unreadable: {error}"))?;
    if state.version != 1 {
        return Err("window claim state has an unsupported format".to_string());
    }
    Ok(state
        .sessions
        .values()
        .flat_map(|session| session.claims.values())
        .any(claim_is_live))
}

fn protocol_claim_is_live(claim: &ClaimRecord) -> bool {
    claim
        .inflight_until_ms
        .unwrap_or(0)
        .max(claim.expires_at_ms)
        > now_ms()
}

pub(crate) fn read_claim_state(path: &Path) -> Result<Option<Vec<u8>>, String> {
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("window claim state is unreadable: {error}")),
    };
    let metadata = file
        .metadata()
        .map_err(|error| format!("window claim state is unreadable: {error}"))?;
    validate_private_file(&metadata, "window claim state")?;
    if metadata.len() > MAX_SERIALIZED_STATE_BYTES as u64 {
        return Err(format!(
            "window claim state exceeds {MAX_SERIALIZED_STATE_BYTES} bytes"
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_SERIALIZED_STATE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("window claim state is unreadable: {error}"))?;
    if bytes.len() > MAX_SERIALIZED_STATE_BYTES {
        return Err(format!(
            "window claim state exceeds {MAX_SERIALIZED_STATE_BYTES} bytes"
        ));
    }
    Ok(Some(bytes))
}

fn validate_private_file(metadata: &fs::Metadata, label: &str) -> Result<(), String> {
    if !metadata.file_type().is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(format!(
            "{label} must be a private regular file owned by the current user"
        ));
    }
    Ok(())
}

fn validate_private_directory(path: &Path, label: &str) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("failed to inspect {label}: {error}"))?;
    if !metadata.file_type().is_dir() || metadata.uid() != unsafe { libc::geteuid() } {
        return Err(format!(
            "{label} must be a private directory owned by the current user"
        ));
    }
    Ok(())
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
#[path = "claim_coordination_tests.rs"]
mod tests;
