import json
import secrets
import time
from pathlib import Path
from typing import Any

from . import kwin


LEASE_FILE = kwin.STATE_DIR / "focus-lease.json"
LEASE_LOCK = kwin.STATE_DIR / "focus-lease.lock"


def _load_lease() -> dict[str, Any] | None:
    if not LEASE_FILE.exists():
        return None
    LEASE_FILE.parent.chmod(0o700)
    LEASE_FILE.chmod(0o600)
    return json.loads(LEASE_FILE.read_text())


def _save_lease(state: dict[str, Any]) -> None:
    LEASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEASE_FILE.parent.chmod(0o700)
    temporary = LEASE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.chmod(0o600)
    temporary.replace(LEASE_FILE)


def _require_lease(token: str) -> dict[str, Any]:
    state = _load_lease()
    if not state:
        raise RuntimeError("no Plasma focus/restoration lease exists")
    if not secrets.compare_digest(str(state.get("token") or ""), token):
        raise ValueError("lease token does not match the Plasma focus/restoration journal")
    return state


def _require_unlocked(operation: str) -> None:
    locked = kwin.screen_locked()
    if locked is not False:
        state = "locked" if locked else "not verifiably unlocked"
        raise RuntimeError(f"{operation} refused because the Plasma session is {state}")


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
    recovery_complete = not errors
    if recovery_complete:
        LEASE_FILE.unlink(missing_ok=True)
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
    }


def _restore(state: dict[str, Any]) -> dict[str, Any]:
    original = state.get("original") or {}
    target = state.get("target") or {}
    errors: list[str] = []
    missing_windows: list[str] = []
    verified: dict[str, bool] = {}
    try:
        _require_unlocked("focus restoration")
    except RuntimeError as exc:
        errors.append(str(exc))
        return _finish_restore(state, errors, missing_windows, verified, None)
    try:
        live_ids = {window["id"] for window in kwin.list_windows()}
    except Exception as exc:
        errors.append(f"window enumeration: {exc}")
        live_ids = set()
    try:
        _require_unlocked("focus restoration")
    except RuntimeError as exc:
        errors.append(str(exc))
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
    return _finish_restore(state, errors, missing_windows, verified, observed_pointer)


def begin_lease(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true; Plasma exposes one shared input seat")
    _require_unlocked("focus lease")
    existing = _load_lease()
    if existing and time.time() > int(existing.get("expires_at") or 0):
        restored = _restore(existing)
        if not restored["recovery_complete"]:
            raise RuntimeError(f"expired focus lease could not be recovered: {restored['errors']}")
    elif existing:
        raise RuntimeError("a Plasma focus/restoration lease already exists; end or recover it first")
    max_seconds = int(arguments.get("max_seconds", 60))
    if not 5 <= max_seconds <= 300:
        raise ValueError("max_seconds must be between 5 and 300")
    target = kwin.resolve_window(str(arguments["window"]))
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
    token = secrets.token_urlsafe(18)
    state = {
        "version": 1,
        "token": token,
        "phase": "prepared",
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + max_seconds,
        "target": target,
        "original": {
            "active_window": original_active,
            "desktop": kwin.current_desktop(),
            "target_desktop": target["desktop"],
            "target_minimized": target["minimized"],
            "pointer": pointer,
        },
    }
    _save_lease(state)
    try:
        _require_unlocked("focus lease")
        kwin.activate(target["id"])
        observed_active = kwin.active_window_id()
        if observed_active != target["id"]:
            raise RuntimeError(f"KWin did not activate the target; observed active window {observed_active}")
        state["phase"] = "active"
        _save_lease(state)
    except Exception as exc:
        restored = _restore(state)
        suffix = "" if restored["recovery_complete"] else f"; restoration also failed: {restored['errors']}"
        raise RuntimeError(f"focus lease activation failed: {exc}{suffix}") from exc
    return {
        "lease_token": token,
        "expires_at": state["expires_at"],
        "window": target,
        "pointer_before": state["original"]["pointer"],
        "next_step": "Call validate_plasma_focus_lease immediately before each separate global-input action. This broker cannot enforce that requirement or gate the external tool.",
        "interference_boundary": "focus, workspace, keyboard, and pointer are shared with the physical Plasma session",
        "external_input_gated_by_broker": False,
        "token_scope": "access to this broker's restoration journal only",
        "deadline_scope": "advisory revalidation and recovery only; it does not disable external input",
    }


def validate_focus_lease(state: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    target_id = str((state.get("target") or {}).get("id") or "")
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
        and not errors
    )
    return {
        "advisory_ready": advisory_ready,
        "phase": state.get("phase"),
        "expired": expired,
        "session_locked": locked,
        "target_live": target_live,
        "target_active": target_active,
        "observed_active_window": active_id,
        "errors": errors,
        "external_input_gated_by_broker": False,
        "required_caller_action": (
            "Revalidate immediately before every companion global-input action. "
            "Do not invoke it when advisory_ready is false. This is caller policy, not broker enforcement."
        ),
    }


