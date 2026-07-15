import re
import secrets
import time
from typing import Any

from . import coordination
from . import kwin


LEASE_FILE = kwin.STATE_DIR / "focus-lease.json"
LEASE_LOCK = kwin.STATE_DIR / "focus-lease.lock"
JOURNAL_LOCK = kwin.STATE_DIR / "focus-lease-journal.lock"
MAX_LEASE_TOKEN_CHARS = 96
LEASE_TOKEN_PATTERN = r"[A-Za-z0-9_-]{24,64}"


def require_lease_token(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_LEASE_TOKEN_CHARS:
        raise ValueError("lease_token is invalid")
    if re.fullmatch(LEASE_TOKEN_PATTERN, value) is None:
        raise ValueError("lease_token is invalid")
    return value


def _binding(state: dict[str, Any]) -> dict[str, Any] | None:
    binding = state.get("binding")
    if binding is None:
        if state.get("version") == 1:
            return None
        raise RuntimeError("focus lease binding is invalid")
    claim = state.get("window_claim")
    owner = state.get("owner")
    target = state.get("target")
    if not isinstance(binding, dict) or not isinstance(claim, dict) or not isinstance(owner, dict) or not isinstance(target, dict):
        raise RuntimeError("focus lease binding is invalid")
    expected = {
        "target_window_id": target.get("id"),
        "owner_thread_id": owner.get("thread_id"),
        "session_identity": state.get("session_identity"),
        "claim_token": claim.get("claim_token"),
    }
    if binding != expected:
        raise RuntimeError("focus lease binding is invalid")
    if claim.get("window_id") != binding["target_window_id"]:
        raise RuntimeError("focus lease binding is invalid")
    coordination.require_window_id(binding.get("target_window_id"))
    coordination.require_thread_id(binding.get("owner_thread_id"))
    coordination.require_claim_token(binding.get("claim_token"))
    if not isinstance(binding.get("session_identity"), dict):
        raise RuntimeError("focus lease binding is invalid")
    require_lease_token(state.get("token"))
    return binding


def _same_lease(expected: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if current is None:
        return False
    expected_token = expected.get("token")
    current_token = current.get("token")
    if not isinstance(expected_token, str) or not isinstance(current_token, str):
        return False
    try:
        expected_binding = _binding(expected)
        current_binding = _binding(current)
    except (RuntimeError, ValueError):
        return False
    if expected_binding is None or current_binding is None:
        return expected_binding is current_binding and expected == current and secrets.compare_digest(
            expected_token,
            current_token,
        )
    expected_identity = {
        "binding": expected_binding,
        "created_at": expected.get("created_at"),
        "expires_at": expected.get("expires_at"),
        "target": expected.get("target"),
        "original": expected.get("original"),
        "window_claim": expected.get("window_claim"),
    }
    current_identity = {
        "binding": current_binding,
        "created_at": current.get("created_at"),
        "expires_at": current.get("expires_at"),
        "target": current.get("target"),
        "original": current.get("original"),
        "window_claim": current.get("window_claim"),
    }
    return expected_identity == current_identity and secrets.compare_digest(expected_token, current_token)


def load() -> dict[str, Any] | None:
    return coordination.read_private_json(LEASE_FILE)


def save(state: dict[str, Any]) -> None:
    _binding(state)
    with kwin.file_guard(JOURNAL_LOCK):
        coordination.write_private_json(LEASE_FILE, state)


def _delete_if_current(state: dict[str, Any]) -> bool:
    with kwin.file_guard(JOURNAL_LOCK):
        current = load()
        if not _same_lease(state, current):
            return False
        LEASE_FILE.unlink(missing_ok=True)
        return True


def recovery_reason(state: dict[str, Any]) -> str | None:
    binding = _binding(state)
    recorded_session = binding["session_identity"] if binding is not None else state.get("session_identity")
    if recorded_session:
        try:
            if recorded_session != coordination.current_session_identity():
                return "different-session"
        except RuntimeError:
            return None
    if time.time() >= int(state.get("expires_at") or 0):
        return "expired"
    owner_process = (state.get("owner") or {}).get("process")
    if owner_process is not None and not coordination.process_is_alive(owner_process):
        return "owner-exited"
    return None


def require(
    token: str,
    owner_id: str | None = None,
    *,
    allow_recovery: bool = False,
) -> dict[str, Any]:
    token = require_lease_token(token)
    if owner_id is not None:
        owner_id = coordination.require_thread_id(owner_id)
    state = load()
    if not state:
        raise RuntimeError("no Plasma focus/restoration lease exists")
    _binding(state)
    recorded_token = state.get("token")
    if not isinstance(recorded_token, str) or not secrets.compare_digest(recorded_token, token):
        raise ValueError("lease token does not match the Plasma focus/restoration journal")
    recorded_owner_value = (state.get("owner") or {}).get("thread_id")
    recorded_owner = coordination.require_thread_id(recorded_owner_value) if recorded_owner_value else ""
    if owner_id is not None and recorded_owner and not secrets.compare_digest(recorded_owner, owner_id):
        reason = recovery_reason(state)
        if not allow_recovery or reason is None:
            raise RuntimeError("the live Plasma focus lease is owned by another agent")
    return state


def require_unlocked(operation: str) -> None:
    locked = kwin.screen_locked()
    if locked is not False:
        state = "locked" if locked else "not verifiably unlocked"
        raise RuntimeError(f"{operation} refused because the Plasma session is {state}")


def _recorded_session_is_current(
    recorded_session: dict[str, Any] | None,
    errors: list[str],
    checkpoint: str,
) -> bool:
    if recorded_session is None:
        return True
    try:
        current_session = coordination.current_session_identity()
    except RuntimeError as exc:
        errors.append(f"session identity {checkpoint}: {exc}")
        return False
    if current_session != recorded_session:
        errors.append(f"KWin session identity changed {checkpoint}")
        return False
    return True


def _finish_restore(
    state: dict[str, Any],
    errors: list[str],
    missing_windows: list[str],
    verified: dict[str, bool],
    observed_pointer: dict[str, int] | None,
) -> dict[str, Any]:
    original = state.get("original") or {}
    active_id = str(original.get("active_window") or "")
    target_desktop = original.get("target_desktop")
    claim = state.get("window_claim") or {}
    claim_released = None
    claim_release_error = None
    recovery_complete = not errors
    if recovery_complete:
        if state.get("token") is not None:
            try:
                deleted = _delete_if_current(state)
            except Exception:
                deleted = False
            if not deleted:
                errors.append("focus lease journal changed before restoration could be finalized")
                recovery_complete = False
        else:
            LEASE_FILE.unlink(missing_ok=True)
    if recovery_complete and claim.get("implicit") is True:
        try:
            claim_released = coordination.discard_bound_claim(
                coordination.require_claim_token(claim.get("claim_token")),
                coordination.require_window_id(claim.get("window_id")),
                coordination.require_thread_id((state.get("owner") or {}).get("thread_id")),
            )
        except Exception as exc:
            claim_release_error = str(exc)
    return {
        "restored": recovery_complete and not missing_windows,
        "recovery_complete": recovery_complete,
        "errors": errors,
        "missing_windows": missing_windows,
        "verified": verified,
        "journal_retained": not recovery_complete,
        "focus_restored_to": active_id if verified.get("focus") else None,
        "desktop_restored_to": original.get("desktop") if verified.get("desktop") else None,
        "target_desktop_restored_to": target_desktop if verified.get("target_desktop") else None,
        "requested_focus_restore": active_id or None,
        "requested_desktop_restore": original.get("desktop"),
        "requested_target_desktop_restore": target_desktop,
        "pointer_restore_required": original.get("pointer") is not None and not verified.get("pointer", False),
        "pointer_restore_coordinate": original.get("pointer"),
        "observed_pointer": observed_pointer,
        "pointer_restored_by_this_backend": False,
        "window_claim_released": claim_released,
        "window_claim_release_error": claim_release_error,
    }


def _stale_restore_result(message: str) -> dict[str, Any]:
    return {
        "restored": False,
        "recovery_complete": False,
        "errors": [message],
        "missing_windows": [],
        "verified": {},
        "journal_retained": LEASE_FILE.exists(),
        "focus_restored_to": None,
        "desktop_restored_to": None,
        "target_desktop_restored_to": None,
        "requested_focus_restore": None,
        "requested_desktop_restore": None,
        "requested_target_desktop_restore": None,
        "pointer_restore_required": False,
        "pointer_restore_coordinate": None,
        "observed_pointer": None,
        "pointer_restored_by_this_backend": False,
        "window_claim_released": None,
        "window_claim_release_error": None,
    }


def restore(state: dict[str, Any]) -> dict[str, Any]:
    _binding(state)
    if state.get("token") is not None:
        try:
            if not _same_lease(state, load()):
                return _stale_restore_result("focus lease journal changed; stale restoration was refused")
        except Exception:
            return _stale_restore_result("focus lease journal could not be verified; stale restoration was refused")
    original = state.get("original") or {}
    target = state.get("target") or {}
    errors: list[str] = []
    missing_windows: list[str] = []
    verified: dict[str, bool] = {}
    recorded_session = state.get("session_identity")
    if recorded_session:
        try:
            current_session = coordination.current_session_identity()
        except RuntimeError as exc:
            errors.append(f"session identity: {exc}")
            return _finish_restore(state, errors, missing_windows, verified, None)
        if recorded_session != current_session:
            missing_windows.append("recorded KWin session is no longer active")
            return _finish_restore(state, errors, missing_windows, verified, None)
    try:
        require_unlocked("focus restoration")
    except RuntimeError as exc:
        errors.append(str(exc))
        return _finish_restore(state, errors, missing_windows, verified, None)
    try:
        live_ids = {window["id"] for window in kwin.list_windows()}
    except Exception as exc:
        errors.append(f"window enumeration: {exc}")
        live_ids = set()
    if not _recorded_session_is_current(recorded_session, errors, "after window enumeration"):
        return _finish_restore(state, errors, missing_windows, verified, None)
    if errors:
        return _finish_restore(state, errors, missing_windows, verified, None)
    try:
        require_unlocked("focus restoration")
    except RuntimeError as exc:
        errors.append(str(exc))
        return _finish_restore(state, errors, missing_windows, verified, None)
    if not _recorded_session_is_current(recorded_session, errors, "immediately before restoration mutations"):
        return _finish_restore(state, errors, missing_windows, verified, None)
    target_id = str(target.get("id") or "")
    target_desktop = original.get("target_desktop")
    if not target_id:
        errors.append("restoration journal has no target window id")
    elif target_id not in live_ids and not errors:
        missing_windows.append(f"target:{target_id}")
    elif target_id in live_ids and target_desktop is not None:
        try:
            kwin.set_window_desktop(target_id, int(target_desktop))
            observed_desktop = kwin.window_desktop(target_id)
            verified["target_desktop"] = observed_desktop == int(target_desktop)
            if not verified["target_desktop"]:
                errors.append(f"target desktop verification: expected {target_desktop}, observed {observed_desktop}")
        except Exception as exc:
            errors.append(f"target desktop restore: {exc}")
        if original.get("target_minimized") is not None:
            try:
                kwin.set_window_minimized(target_id, bool(original["target_minimized"]))
                observed_minimized = kwin.window_boolean(target_id, "minimized")
                verified["target_minimized"] = observed_minimized is bool(original["target_minimized"])
                if not verified["target_minimized"]:
                    errors.append(
                        "target minimized-state verification: "
                        f"expected {original['target_minimized']}, observed {observed_minimized}"
                    )
            except Exception as exc:
                errors.append(f"target minimized-state restore: {exc}")
    if original.get("desktop") is not None:
        try:
            kwin.set_desktop(int(original["desktop"]))
        except Exception as exc:
            errors.append(f"desktop restore: {exc}")
    active_id = str(original.get("active_window") or "")
    if not active_id:
        errors.append("restoration journal has no original active window id")
    elif active_id not in live_ids and not errors:
        missing_windows.append(f"original-active:{active_id}")
    elif active_id in live_ids:
        try:
            kwin.activate(active_id)
        except Exception as exc:
            errors.append(f"focus restore: {exc}")
    if original.get("desktop") is not None:
        try:
            observed_desktop = kwin.current_desktop()
            verified["desktop"] = observed_desktop == int(original["desktop"])
            if not verified["desktop"]:
                errors.append(f"desktop verification: expected {original['desktop']}, observed {observed_desktop}")
        except Exception as exc:
            errors.append(f"desktop verification: {exc}")
    if active_id in live_ids:
        try:
            observed_active = kwin.active_window_id()
            verified["focus"] = observed_active == active_id
            if not verified["focus"]:
                errors.append(f"focus verification: expected {active_id}, observed {observed_active}")
        except Exception as exc:
            errors.append(f"focus verification: {exc}")
    original_pointer = original.get("pointer")
    observed_pointer = None
    if original_pointer is not None:
        try:
            observed_pointer = kwin.pointer_position()
            verified["pointer"] = observed_pointer == original_pointer
            if not verified["pointer"]:
                errors.append(f"pointer verification: expected {original_pointer}, observed {observed_pointer}")
        except Exception as exc:
            errors.append(f"pointer verification: {exc}")
    if not errors:
        _recorded_session_is_current(recorded_session, errors, "before restoration finalization")
    return _finish_restore(state, errors, missing_windows, verified, observed_pointer)


def begin(arguments: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
    owner_id = coordination.require_thread_id(owner_id or coordination.legacy_owner_id())
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true; Plasma exposes one shared input seat")
    require_unlocked("focus lease")
    existing = load()
    if existing and recovery_reason(existing) is not None:
        restored = restore(existing)
        if not restored["recovery_complete"]:
            raise RuntimeError(f"stale focus lease could not be recovered: {restored['errors']}")
    elif existing:
        raise RuntimeError("the global Plasma input seat has a live focus lease; end it before beginning another")
    max_seconds = arguments.get("max_seconds", 60)
    if type(max_seconds) is not int or not 5 <= max_seconds <= 300:
        raise ValueError("max_seconds must be between 5 and 300")
    session = coordination.current_session_identity()
    target = kwin.resolve_window(coordination.require_window_query(arguments.get("window")))
    if target.get("desktop") is None:
        raise RuntimeError("focus lease refused because the target desktop could not be queried")
    if target.get("minimized") is None:
        raise RuntimeError("focus lease refused because KWin minimized state could not be read through qdbus")
    pointer = kwin.pointer_position()
    if pointer is None:
        raise RuntimeError("focus lease refused because the physical pointer position could not be journaled")
    original_active = kwin.active_window_id()
    if original_active is None or not kwin.window_info(original_active):
        raise RuntimeError("focus lease refused because the original active window could not be positively identified")
    original_desktop = kwin.current_desktop()
    if coordination.current_session_identity() != session:
        raise RuntimeError("KWin session identity changed while snapshotting the focus lease")
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    target_for_model = coordination.window_for_model(target)
    state = None
    with coordination.focus_claim_transaction(
        target_for_model,
        owner_id,
        arguments.get("claim_token"),
        max_seconds,
        session,
    ) as claim:
        if claim.get("session_identity") != session:
            raise RuntimeError("focus claim session identity does not match the snapshotted KWin session")
        state = {
            "version": 3,
            "token": token,
            "phase": "prepared",
            "created_at": now,
            "expires_at": now + max_seconds,
            "session_identity": session,
            "owner": {"thread_id": owner_id, "process": coordination.current_process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": target["id"],
                "implicit": bool(claim.get("implicit")),
            },
            "target": target_for_model,
            "original": {
                "active_window": original_active,
                "desktop": original_desktop,
                "target_desktop": target["desktop"],
                "target_minimized": target["minimized"],
                "pointer": pointer,
            },
        }
        state["binding"] = {
            "target_window_id": target_for_model["id"],
            "owner_thread_id": owner_id,
            "session_identity": session,
            "claim_token": claim["claim_token"],
        }
        save(state)
    assert state is not None
    try:
        coordination.authorize_window(target["id"], owner_id, claim["claim_token"])
        require_unlocked("focus lease")
        if coordination.current_session_identity() != state["session_identity"]:
            raise RuntimeError("KWin session identity changed while preparing the focus lease")
        kwin.activate(target["id"])
        observed_active = kwin.active_window_id()
        if observed_active != target["id"]:
            raise RuntimeError(f"KWin did not activate the target; observed active window {observed_active}")
        state["phase"] = "active"
        save(state)
    except Exception as exc:
        restored = restore(state)
        suffix = "" if restored["recovery_complete"] else f"; restoration also failed: {restored['errors']}"
        raise RuntimeError(f"focus lease activation failed: {exc}{suffix}") from exc
    return {
        "lease_token": token,
        "expires_at": state["expires_at"],
        "window": target_for_model,
        "pointer_before": state["original"]["pointer"],
        "next_step": "Call validate_plasma_focus_lease immediately before each separate global-input action. This broker cannot enforce that requirement or gate the external tool.",
        "interference_boundary": "focus, workspace, keyboard, and pointer are shared with the physical Plasma session",
        "global_seat_serialized_by_broker": True,
        "window_claim": {
            "claim_token": claim["claim_token"],
            "implicit": bool(claim.get("implicit")),
            "expires_at": claim["expires_at"],
        },
        "external_input_gated_by_broker": False,
        "token_scope": "this thread's broker-owned global-seat and restoration journal only",
        "deadline_scope": "advisory revalidation and recovery only; it does not disable external input",
    }


def validate(state: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
    _binding(state)
    owner_id = coordination.require_thread_id(
        owner_id or (state.get("owner") or {}).get("thread_id") or coordination.legacy_owner_id()
    )
    errors: list[str] = []
    target_id = str((state.get("target") or {}).get("id") or "")
    claim = state.get("window_claim") or {}
    claim_active = True
    if claim:
        try:
            claim_active = coordination.authorize_window(
                target_id,
                owner_id,
                coordination.require_claim_token(claim.get("claim_token")),
            ) is not None
        except Exception as exc:
            claim_active = False
            errors.append(f"window claim: {exc}")
    try:
        live_ids = {window["id"] for window in kwin.list_windows()}
    except Exception as exc:
        errors.append(f"window enumeration: {exc}")
        live_ids = set()
    locked = kwin.screen_locked()
    active_id = kwin.active_window_id()
    expired = time.time() >= int(state.get("expires_at") or 0)
    target_live = target_id in live_ids
    target_active = bool(target_id) and active_id == target_id
    advisory_ready = (
        state.get("phase") == "active"
        and not expired
        and locked is False
        and target_live
        and target_active
        and claim_active
        and not errors
    )
    return {
        "advisory_ready": advisory_ready,
        "phase": state.get("phase"),
        "expired": expired,
        "session_locked": locked,
        "target_live": target_live,
        "target_active": target_active,
        "window_claim_active": claim_active,
        "observed_active_window": active_id,
        "errors": errors,
        "global_seat_serialized_by_broker": True,
        "external_input_gated_by_broker": False,
        "required_caller_action": (
            "Revalidate immediately before every companion global-input action. "
            "Begin that one action only when advisory_ready is true. The broker serializes its focus leases, "
            "but the separate input plugin cannot consume this token, so this boundary remains caller-enforced."
        ),
    }
