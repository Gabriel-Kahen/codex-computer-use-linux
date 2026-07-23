use crate::claim_coordination::{
    constant_time_eq, now_ms, open_lock, prepare_state_dir, read_claim_state, ClaimContext,
};
use crate::coordination_identity::{self, CoordinationScope};
use crate::coordination_protocol::{
    ClaimRecord, ClaimSession, ClaimState, ClaimWindow, MAX_LEASE_SECONDS, MIN_LEASE_SECONDS,
    PROTOCOL_VERSION,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use fs2::FileExt;
use schemars::JsonSchema;
use serde::Serialize;
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
pub(crate) struct ClaimReceipt {
    pub(crate) owner_thread_id: String,
    pub(crate) claim_token: Option<String>,
    pub(crate) fencing_token: Option<u64>,
    pub(crate) expires_at_ms: Option<u64>,
}
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub(crate) struct ListedClaim {
    window_id: String,
    owner_thread_id: String,
    owned_by_caller: bool,
    expires_at_ms: u64,
}
enum Mutation {
    Claim { lease_seconds: u32 },
    Renew { token: String, lease_seconds: u32 },
}
pub(crate) async fn claim_window(
    window_id: u64,
    owner: String,
    lease_seconds: u32,
) -> Result<ClaimReceipt, String> {
    mutate(window_id, owner, Mutation::Claim { lease_seconds }).await
}
pub(crate) async fn renew_window_claim(
    window_id: u64,
    owner: String,
    token: String,
    lease_seconds: u32,
) -> Result<ClaimReceipt, String> {
    mutate(
        window_id,
        owner,
        Mutation::Renew {
            token,
            lease_seconds,
        },
    )
    .await
}
pub(crate) async fn release_window_claim(
    owner: String,
    token: String,
) -> Result<ClaimReceipt, String> {
    let state_dir = coordination_identity::state_dir()
        .ok_or("cannot locate the coordination state directory")?;
    tokio::task::spawn_blocking(move || release_locked(&state_dir, owner, token))
        .await
        .map_err(|error| format!("window claim transaction failed: {error}"))?
}
pub(crate) async fn list_window_claims(
    owner: String,
    cursor: Option<String>,
) -> Result<(Vec<ListedClaim>, Option<String>), String> {
    if cursor.as_ref().is_some_and(|value| {
        value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err("cursor must be a lowercase 64-character window key".to_string());
    }
    let scope = coordination_identity::resolve(None).await?;
    if scope.legacy_hyprland_binding.is_some() {
        return Err("Hyprland claims remain companion-owned during migration".to_string());
    }
    tokio::task::spawn_blocking(move || {
        prepare_state_dir(&scope.state_dir)?;
        let lock = open_lock(&scope.state_dir.join("window-claims.lock"))?;
        FileExt::lock_shared(&lock).map_err(|error| format!("failed to lock claims: {error}"))?;
        let state = load_state(&scope.state_dir)?;
        let mut claims = state
            .sessions
            .get(&scope.session.key())
            .into_iter()
            .flat_map(|session| session.claims.iter())
            .filter(|(key, claim)| {
                cursor.as_ref().is_none_or(|cursor| *key > cursor)
                    && claim_deadline(claim) > now_ms()
            })
            .take(9)
            .map(|(key, claim)| {
                (
                    key.clone(),
                    ListedClaim {
                        window_id: claim.window.identity.id.clone(),
                        owner_thread_id: claim.owner_thread_id.clone(),
                        owned_by_caller: claim.owner_thread_id == owner,
                        expires_at_ms: claim_deadline(claim),
                    },
                )
            })
            .collect::<Vec<_>>();
        let next = (claims.len() > 8).then(|| claims[7].0.clone());
        claims.truncate(8);
        Ok((claims.into_iter().map(|(_, claim)| claim).collect(), next))
    })
    .await
    .map_err(|error| format!("window claim listing failed: {error}"))?
}
async fn mutate(window_id: u64, owner: String, mutation: Mutation) -> Result<ClaimReceipt, String> {
    let scope = coordination_identity::resolve(Some(window_id)).await?;
    if scope.legacy_hyprland_binding.is_some() {
        return Err(
            "Hyprland claims remain owned by the same-session companion during migration"
                .to_string(),
        );
    }
    tokio::task::spawn_blocking(move || mutate_locked(scope, owner, mutation))
        .await
        .map_err(|error| format!("window claim transaction failed: {error}"))?
}
fn mutate_locked(
    scope: CoordinationScope,
    owner: String,
    mutation: Mutation,
) -> Result<ClaimReceipt, String> {
    validate_lease(&mutation)?;
    let context = ClaimContext {
        owner_thread_id: Some(owner.clone()),
        claim_token: mutation.token().map(str::to_string),
    };
    crate::claim_coordination::validate_context(&context)?;
    prepare_state_dir(&scope.state_dir)?;
    let window = scope
        .window
        .as_ref()
        .ok_or("window identity is required for a claim transaction")?;
    let window_key = window.key(&scope.session);
    let window_lock = open_lock(
        &scope
            .state_dir
            .join("window-locks")
            .join(format!("{window_key}.lock")),
    )?;
    FileExt::lock_exclusive(&window_lock)
        .map_err(|error| format!("failed to lock target window: {error}"))?;
    let journal_lock = open_lock(&scope.state_dir.join("window-claims.lock"))?;
    FileExt::lock_exclusive(&journal_lock)
        .map_err(|error| format!("failed to lock window claims: {error}"))?;
    let mut state = load_state(&scope.state_dir)?;
    let now = now_ms();
    for session in state.sessions.values_mut() {
        session
            .claims
            .retain(|_, claim| claim_deadline(claim) > now);
    }
    let session_key = scope.session.key();
    state
        .sessions
        .retain(|key, session| key == &session_key || !session.claims.is_empty());
    let session = state
        .sessions
        .entry(session_key)
        .or_insert_with(|| ClaimSession {
            identity: scope.session.clone(),
            next_fencing_token: 1,
            claims: BTreeMap::new(),
        });
    if session.identity != scope.session {
        return Err("window claim session identity mismatch".to_string());
    }
    let receipt = match mutation {
        Mutation::Claim { lease_seconds } => {
            if session.claims.contains_key(&window_key) {
                return Err(
                    "window is already actively claimed; renew it with its token".to_string(),
                );
            }
            let fencing_token = session.next_fencing_token;
            session.next_fencing_token = fencing_token
                .checked_add(1)
                .ok_or("window claim fencing token is exhausted")?;
            let claim_token = random_token()?;
            let expires_at_ms = expiry(now, lease_seconds)?;
            session.claims.insert(
                window_key,
                ClaimRecord {
                    owner_thread_id: owner.clone(),
                    claim_token: claim_token.clone(),
                    fencing_token,
                    owner_process: None,
                    claimed_at_ms: now,
                    renewed_at_ms: now,
                    expires_at_ms,
                    lease_seconds,
                    inflight_until_ms: None,
                    window: ClaimWindow {
                        identity: window.clone(),
                        summary: BTreeMap::new(),
                    },
                },
            );
            ClaimReceipt {
                owner_thread_id: owner,
                claim_token: Some(claim_token),
                fencing_token: Some(fencing_token),
                expires_at_ms: Some(expires_at_ms),
            }
        }
        Mutation::Renew {
            token,
            lease_seconds,
        } => {
            let claim = authorized_claim(session, &window_key, &owner, &token)?;
            claim.renewed_at_ms = now;
            claim.expires_at_ms = expiry(now, lease_seconds)?;
            claim.lease_seconds = lease_seconds;
            claim.inflight_until_ms = None;
            ClaimReceipt {
                owner_thread_id: owner,
                claim_token: Some(token),
                fencing_token: Some(claim.fencing_token),
                expires_at_ms: Some(claim.expires_at_ms),
            }
        }
    };
    state.validate()?;
    write_state(&scope.state_dir, &state)?;
    Ok(receipt)
}
fn release_locked(state_dir: &Path, owner: String, token: String) -> Result<ClaimReceipt, String> {
    let context = ClaimContext {
        owner_thread_id: Some(owner.clone()),
        claim_token: Some(token.clone()),
    };
    crate::claim_coordination::validate_context(&context)?;
    prepare_state_dir(state_dir)?;
    let journal_path = state_dir.join("window-claims.lock");
    let journal_lock = open_lock(&journal_path)?;
    FileExt::lock_shared(&journal_lock)
        .map_err(|error| format!("failed to lock window claims: {error}"))?;
    let state = load_state(state_dir)?;
    let now = now_ms();
    let found = state.sessions.iter().find_map(|(session_key, session)| {
        session.claims.iter().find_map(|(window_key, claim)| {
            (claim_deadline(claim) > now
                && claim.owner_thread_id == owner
                && constant_time_eq(&claim.claim_token, &token))
            .then(|| (session_key.clone(), window_key.clone()))
        })
    });
    drop(journal_lock);
    let Some((session_key, window_key)) = found else {
        return Ok(released_receipt(owner, None));
    };
    let window_lock = open_lock(
        &state_dir
            .join("window-locks")
            .join(format!("{window_key}.lock")),
    )?;
    FileExt::lock_exclusive(&window_lock)
        .map_err(|error| format!("failed to lock target window: {error}"))?;
    let journal_lock = open_lock(&journal_path)?;
    FileExt::lock_exclusive(&journal_lock)
        .map_err(|error| format!("failed to lock window claims: {error}"))?;
    let mut state = load_state(state_dir)?;
    let Some(claim) = state
        .sessions
        .get(&session_key)
        .and_then(|session| session.claims.get(&window_key))
    else {
        return Ok(released_receipt(owner, None));
    };
    if claim.owner_thread_id != owner || !constant_time_eq(&claim.claim_token, &token) {
        return Err("window claim belongs to another owner or token".to_string());
    }
    let fencing_token = claim.fencing_token;
    state
        .sessions
        .get_mut(&session_key)
        .expect("session was just checked")
        .claims
        .remove(&window_key);
    state.validate()?;
    write_state(state_dir, &state)?;
    Ok(released_receipt(owner, Some(fencing_token)))
}
fn released_receipt(owner_thread_id: String, fencing_token: Option<u64>) -> ClaimReceipt {
    ClaimReceipt {
        owner_thread_id,
        claim_token: None,
        fencing_token,
        expires_at_ms: None,
    }
}
fn authorized_claim<'a>(
    session: &'a mut ClaimSession,
    window_key: &str,
    owner: &str,
    token: &str,
) -> Result<&'a mut ClaimRecord, String> {
    let claim = session
        .claims
        .get_mut(window_key)
        .ok_or("window claim is missing or expired")?;
    if claim.owner_thread_id != owner || !constant_time_eq(&claim.claim_token, token) {
        return Err("window claim belongs to another owner or token".to_string());
    }
    Ok(claim)
}
fn load_state(state_dir: &Path) -> Result<ClaimState, String> {
    let Some(bytes) = read_claim_state(&state_dir.join("window-claims.json"))? else {
        return Ok(ClaimState {
            version: PROTOCOL_VERSION,
            sessions: BTreeMap::new(),
        });
    };
    let state: ClaimState = serde_json::from_slice(&bytes)
        .map_err(|error| format!("window claim state is unreadable: {error}"))?;
    state.validate()?;
    Ok(state)
}
fn write_state(state_dir: &Path, state: &ClaimState) -> Result<(), String> {
    let path = state_dir.join("window-claims.json");
    let temporary = state_dir.join(format!(
        ".window-claims.{}.{}.tmp",
        std::process::id(),
        random_token()?
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(|error| format!("failed to create claim state: {error}"))?;
        serde_json::to_writer(&mut file, state)
            .map_err(|error| format!("failed to encode claim state: {error}"))?;
        file.write_all(b"\n")
            .map_err(|error| format!("failed to finish claim state: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("failed to sync claim state: {error}"))?;
        fs::rename(&temporary, &path)
            .map_err(|error| format!("failed to install claim state: {error}"))?;
        File::open(state_dir)
            .and_then(|directory| directory.sync_all())
            .map_err(|error| format!("failed to sync claim directory: {error}"))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}
fn random_token() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("secure randomness is unavailable: {error}"))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}
fn validate_lease(mutation: &Mutation) -> Result<(), String> {
    let seconds = match mutation {
        Mutation::Claim { lease_seconds } | Mutation::Renew { lease_seconds, .. } => lease_seconds,
    };
    if !(MIN_LEASE_SECONDS..=MAX_LEASE_SECONDS).contains(seconds) {
        return Err(format!(
            "lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        ));
    }
    Ok(())
}
fn expiry(now: u64, lease_seconds: u32) -> Result<u64, String> {
    now.checked_add(u64::from(lease_seconds) * 1000)
        .ok_or("window claim expiry overflows".to_string())
}
fn claim_deadline(claim: &ClaimRecord) -> u64 {
    claim
        .inflight_until_ms
        .unwrap_or(0)
        .max(claim.expires_at_ms)
}
impl Mutation {
    fn token(&self) -> Option<&str> {
        match self {
            Self::Claim { .. } => None,
            Self::Renew { token, .. } => Some(token),
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::coordination_protocol::{
        DesktopBackend, IdentityAttribute, SessionIdentity, WindowIdentity,
    };
    use std::sync::atomic::{AtomicU64, Ordering};
    fn scope() -> CoordinationScope {
        static NEXT: AtomicU64 = AtomicU64::new(1);
        CoordinationScope {
            state_dir: std::env::temp_dir().join(format!(
                "computer-use-claim-lifecycle-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            )),
            session: SessionIdentity {
                backend: DesktopBackend::Gnome,
                uid: unsafe { libc::geteuid() },
                attributes: BTreeMap::from([(
                    "bus_id".to_string(),
                    IdentityAttribute::Text("test-bus".to_string()),
                )]),
            },
            window: Some(WindowIdentity {
                backend: DesktopBackend::Gnome,
                id: "window-42".to_string(),
                process: None,
            }),
            legacy_hyprland_binding: None,
        }
    }
    fn claim(scope: &CoordinationScope, owner: &str) -> Result<ClaimReceipt, String> {
        mutate_locked(
            scope.clone(),
            owner.to_string(),
            Mutation::Claim { lease_seconds: 60 },
        )
    }
    #[test]
    fn lifecycle_is_recoverable_authenticated_and_monotonic() {
        let scope = scope();
        let first = claim(&scope, "owner-a").unwrap();
        let token = first.claim_token.clone().unwrap();
        assert_eq!(first.fencing_token, Some(1));
        assert!(claim(&scope, "owner-a").is_err());
        assert!(mutate_locked(
            scope.clone(),
            "owner-b".to_string(),
            Mutation::Renew {
                token: token.clone(),
                lease_seconds: 60,
            },
        )
        .is_err());
        release_locked(&scope.state_dir, "owner-a".to_string(), token.clone()).unwrap();
        let repeated = release_locked(&scope.state_dir, "owner-a".to_string(), token).unwrap();
        assert_eq!(repeated.fencing_token, None);
        let second = claim(&scope, "owner-a").unwrap();
        assert_eq!(second.fencing_token, Some(2));
        load_state(&scope.state_dir).unwrap().validate().unwrap();
        fs::remove_dir_all(scope.state_dir).unwrap();
    }
}
