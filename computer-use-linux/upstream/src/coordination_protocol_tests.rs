use super::*;

fn fixture() -> ClaimState {
    serde_json::from_str(include_str!(
        "../tests/fixtures/coordination/v2_claim_state.json"
    ))
    .unwrap()
}

#[test]
fn canonical_fixture_has_stable_cross_language_keys() {
    let state = fixture();
    let (session_key, session) = state.sessions.first_key_value().unwrap();
    let (window_key, claim) = session.claims.first_key_value().unwrap();

    assert_eq!(
        session_key,
        "b8f42adc1ada5977b883432ff84b34d8a065bcf056c040cb32b0d60e40b8e68d"
    );
    assert_eq!(
        window_key,
        "072e2275d7382f5a1e0320c95de3be3cc7295a13713aaa24f1a7911ded631ae4"
    );
    assert_eq!(session_key, &session.identity.key());
    assert_eq!(window_key, &claim.window.identity.key(&session.identity));
    state.validate().unwrap();
}

#[test]
fn rejects_identity_and_claim_key_mismatches() {
    let mut state = fixture();
    let (_, session) = state.sessions.pop_first().unwrap();
    state
        .sessions
        .insert("wrong-session-key".to_string(), session);
    assert_eq!(
        state.validate().unwrap_err(),
        "coordination session key does not match its identity"
    );

    let mut state = fixture();
    let session = state.sessions.values_mut().next().unwrap();
    let (_, claim) = session.claims.pop_first().unwrap();
    session.claims.insert("wrong-window-key".to_string(), claim);
    assert_eq!(
        state.validate().unwrap_err(),
        "coordination window key does not match its identity"
    );
}

#[test]
fn rejects_cross_backend_claims_and_invalid_deadlines() {
    let mut state = fixture();
    let claim = state
        .sessions
        .values_mut()
        .next()
        .unwrap()
        .claims
        .values_mut()
        .next()
        .unwrap();
    claim.window.identity.backend = DesktopBackend::Gnome;
    assert_eq!(
        state.validate().unwrap_err(),
        "window and session backends do not match"
    );

    let mut state = fixture();
    let claim = state
        .sessions
        .values_mut()
        .next()
        .unwrap()
        .claims
        .values_mut()
        .next()
        .unwrap();
    claim.expires_at_ms = u64::MAX;
    assert_eq!(
        state.validate().unwrap_err(),
        "claim contains an invalid deadline"
    );
}

#[test]
fn rejects_unknown_wire_fields() {
    let mut value: serde_json::Value = serde_json::from_str(include_str!(
        "../tests/fixtures/coordination/v2_claim_state.json"
    ))
    .unwrap();
    value["unexpected"] = serde_json::json!(true);
    assert!(serde_json::from_value::<ClaimState>(value).is_err());
}

#[test]
fn rejects_unbounded_inflight_deadlines() {
    let mut state = fixture();
    let claim = state
        .sessions
        .values_mut()
        .next()
        .unwrap()
        .claims
        .values_mut()
        .next()
        .unwrap();
    claim.inflight_until_ms = Some(u64::MAX);
    assert_eq!(
        state.validate().unwrap_err(),
        "claim contains an invalid deadline"
    );
}

#[test]
fn rejects_reused_or_non_monotonic_fencing_tokens() {
    let mut state = fixture();
    let session = state.sessions.values_mut().next().unwrap();
    session.next_fencing_token = 7;
    assert_eq!(
        state.validate().unwrap_err(),
        "next_fencing_token must be greater than every issued token"
    );

    let mut state = fixture();
    let session = state.sessions.values_mut().next().unwrap();
    let mut claim = session.claims.values().next().unwrap().clone();
    claim.window.identity.id = "address:0x2b".to_string();
    let key = claim.window.identity.key(&session.identity);
    session.claims.insert(key, claim);
    assert_eq!(
        state.validate().unwrap_err(),
        "coordination session contains a duplicate fencing token"
    );
}
