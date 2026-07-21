import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .native_plugin import STATE_DIR
from .native_plugin import file_guard


CLAIMS_FILE = STATE_DIR / "window-claims.json"
CLAIMS_LOCK_FILE = STATE_DIR / "window-claims.lock"
WINDOW_LOCK_DIR = STATE_DIR / "window-locks"
# Keep the 0.1.x filename so rolling broker upgrades share the same global lane.
GLOBAL_INPUT_LOCK_FILE = STATE_DIR / "pointer-transaction.lock"
DEFAULT_LEASE_SECONDS = 60
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
MAX_ACTIVE_CLAIMS = 128
MAX_OWNER_LENGTH = 128
MAX_CLAIM_TOKEN_LENGTH = 128
MAX_INFLIGHT_SECONDS = 300

def binding_key(binding: dict[str, Any]) -> str:
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def window_key(window: dict[str, Any]) -> str:
    capture_id = window.get("capture_id")
    if capture_id is not None:
        return f"capture:{capture_id}"
    address = str(window.get("address") or "")
    if not address:
        raise RuntimeError("selected window has neither an exact-capture identifier nor an address")
    return f"address:{address}"


def window_lock_key(window: dict[str, Any]) -> str:
    address = str(window.get("address") or "")
    if not address:
        raise RuntimeError("selected window has no compositor address")
    return f"address:{address}"


def _lock_key(binding: dict[str, Any], key: str) -> str:
    return hashlib.sha256(f"{binding_key(binding)}\0{key}".encode()).hexdigest()


@contextmanager
def _window_guard_for_key(binding: dict[str, Any], key: str):
    lock_key = _lock_key(binding, key)
    with file_guard(WINDOW_LOCK_DIR / f"{lock_key}.lock"):
        yield


@contextmanager
def window_guard(binding: dict[str, Any], window: dict[str, Any]):
    """Serialize observe/mutate transactions for one compositor window."""
    with _window_guard_for_key(binding, window_lock_key(window)):
        yield


@contextmanager
def global_input_guard():
    """Serialize XWayland, physical-seat, and fallback transactions."""
    with file_guard(GLOBAL_INPUT_LOCK_FILE):
        yield


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load_state() -> dict[str, Any]:
    if not CLAIMS_FILE.exists():
        return {"version": 1, "sessions": {}}
    try:
        state = json.loads(CLAIMS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"window claim state is unreadable: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("sessions"), dict):
        raise RuntimeError("window claim state has an unsupported format")
    return state


def _session_claims(
    state: dict[str, Any], binding: dict[str, Any], *, create: bool
) -> dict[str, dict[str, Any]]:
    key = binding_key(binding)
    session = state["sessions"].get(key)
    if session is None:
        if not create:
            return {}
        session = {"binding": binding, "claims": {}}
        state["sessions"][key] = session
    if session.get("binding") != binding or not isinstance(session.get("claims"), dict):
        raise RuntimeError("window claim state does not match the active display and Hyprland instance")
    return session["claims"]


def _prune_expired(state: dict[str, Any], now: float) -> bool:
    changed = False
    for key, session in list(state["sessions"].items()):
        claims = session.get("claims")
        if not isinstance(claims, dict):
            raise RuntimeError("window claim state is malformed")
        for claimed_window, claim in list(claims.items()):
            live_until = max(
                float(claim.get("expires_at") or 0),
                float(claim.get("inflight_until") or 0),
            )
            if live_until <= now:
                del claims[claimed_window]
                changed = True
        if not claims:
            del state["sessions"][key]
            changed = True
    return changed


def _window_summary(window: dict[str, Any]) -> dict[str, Any]:
    limits = {"address": 64, "capture_id": 64, "class": 80, "title": 160}
    return {
        key: str(window[key])[:limit] if window.get(key) is not None else None
        for key, limit in limits.items()
    }


def _public_claim(claim: dict[str, Any], *, include_token: bool) -> dict[str, Any]:
    owner = claim.get("owner_thread_id")
    if not isinstance(owner, str) or not owner or len(owner) > MAX_OWNER_LENGTH:
        raise RuntimeError("window claim state contains an invalid owner")
    result = {
        "window": _window_summary(claim.get("window") or {}),
        "owner_thread_id": owner,
        "claimed_at": float(claim["claimed_at"]),
        "expires_at": float(claim["expires_at"]),
        "lease_seconds": int(claim["lease_seconds"]),
    }
    if include_token:
        result["claim_token"] = claim["claim_token"]
    return result


def _find_claim_by_token(
    claims: dict[str, dict[str, Any]], token: str
) -> tuple[dict[str, Any], str] | None:
    for claimed_window, claim in claims.items():
        if secrets.compare_digest(str(claim.get("claim_token") or ""), token):
            return claim, claimed_window
    return None


def claim_window(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    *,
    reservation_owner: Callable[[], str | None] | None = None,
    after_claim: Callable[[dict[str, Any]], None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(owner_thread_id, str)
        or not owner_thread_id.strip()
        or len(owner_thread_id) > MAX_OWNER_LENGTH
    ):
        raise ValueError(f"owner_thread_id must contain 1..{MAX_OWNER_LENGTH} characters")
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )
    current_time = time.time() if now is None else now
    key = window_key(window)
    lock_key = window_lock_key(window)
    with window_guard(binding, window):
        reserved_by = reservation_owner() if reservation_owner else None
        if reserved_by is not None and reserved_by != owner_thread_id:
            raise RuntimeError("window is reserved by another agent's active coordinate lease")
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time)
            claims = _session_claims(state, binding, create=True)
            key = next(
                (
                    claimed_window
                    for claimed_window, candidate in claims.items()
                    if window_lock_key(candidate.get("window") or {}) == lock_key
                ),
                key,
            )
            existing = claims.get(key)
            if existing and existing.get("owner_thread_id") != owner_thread_id:
                raise RuntimeError(
                    f"window is claimed by another computer-use agent until {float(existing['expires_at'])}"
                )
            if existing is None and len(claims) >= MAX_ACTIVE_CLAIMS:
                raise RuntimeError(
                    f"active window claim limit ({MAX_ACTIVE_CLAIMS}) reached for this session"
                )
            renewed = existing is not None
            claim = existing or {
                "claim_token": secrets.token_urlsafe(24),
                "owner_thread_id": owner_thread_id,
                "claimed_at": current_time,
            }
            claim.update(
                {
                    "window": _window_summary(window),
                    "lease_seconds": lease_seconds,
                    "expires_at": current_time + lease_seconds,
                }
            )
            if after_claim:
                claim["inflight_until"] = current_time + MAX_INFLIGHT_SECONDS
            claims[key] = claim
            atomic_write_json(CLAIMS_FILE, state)
            result = {**_public_claim(claim, include_token=True), "renewed": renewed}
        if after_claim:
            try:
                after_claim(result)
            except Exception:
                with file_guard(CLAIMS_LOCK_FILE):
                    state = _load_state()
                    claim = _session_claims(state, binding, create=False).get(key)
                    if claim is not None:
                        claim.pop("inflight_until", None)
                        atomic_write_json(CLAIMS_FILE, state)
                raise
            completed_at = time.time() if now is None else now
            with file_guard(CLAIMS_LOCK_FILE):
                state = _load_state()
                claim = _session_claims(state, binding, create=False).get(key)
                if (
                    claim is None
                    or claim.get("owner_thread_id") != owner_thread_id
                    or not secrets.compare_digest(
                        str(claim.get("claim_token") or ""),
                        str(result["claim_token"]),
                    )
                ):
                    raise RuntimeError("window claim changed during coordinate synchronization")
                claim.pop("inflight_until", None)
                claim["expires_at"] = completed_at + lease_seconds
                atomic_write_json(CLAIMS_FILE, state)
                result = {
                    **_public_claim(claim, include_token=True),
                    "renewed": renewed,
                }
        return result


def release_claim(
    binding: dict[str, Any],
    claim_token: str,
    owner_thread_id: str,
    *,
    before_release: Callable[[dict[str, Any]], None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(claim_token, str)
        or not claim_token
        or len(claim_token) > MAX_CLAIM_TOKEN_LENGTH
    ):
        raise ValueError(
            f"claim_token must contain 1..{MAX_CLAIM_TOKEN_LENGTH} characters"
        )
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claims = _session_claims(state, binding, create=False)
        found = _find_claim_by_token(claims, claim_token)
        if found is None:
            if changed:
                atomic_write_json(CLAIMS_FILE, state)
            return {"released": False}
        claim, key = found
        if claim.get("owner_thread_id") != owner_thread_id:
            raise RuntimeError("only the agent that owns a live window claim may release it")

    with _window_guard_for_key(binding, window_lock_key(claim.get("window") or {})):
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time)
            claims = _session_claims(state, binding, create=False)
            claim = claims.get(key)
            if claim is None or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            ):
                atomic_write_json(CLAIMS_FILE, state)
                return {"released": False}
            if claim.get("owner_thread_id") != owner_thread_id:
                raise RuntimeError("only the agent that owns a live window claim may release it")
            claim_snapshot = claim.copy()
        if before_release:
            before_release(claim_snapshot)
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time)
            claims = _session_claims(state, binding, create=False)
            claim = claims.get(key)
            if claim is None or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            ):
                atomic_write_json(CLAIMS_FILE, state)
                return {"released": False}
            if claim.get("owner_thread_id") != owner_thread_id:
                raise RuntimeError("only the agent that owns a live window claim may release it")
            del claims[key]
            _prune_expired(state, current_time)
            atomic_write_json(CLAIMS_FILE, state)
            return {
                "released": True,
                "window": _window_summary(claim.get("window") or {}),
            }


def list_claims(binding: dict[str, Any], *, now: float | None = None) -> list[dict[str, Any]]:
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claims = _session_claims(state, binding, create=False)
        result = [_public_claim(claim, include_token=False) for claim in claims.values()]
        if changed:
            atomic_write_json(CLAIMS_FILE, state)
        return sorted(
            result, key=lambda claim: str(claim["window"].get("address") or "")
        )[:MAX_ACTIVE_CLAIMS]


def require_window_access(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str | None,
    claim_token: str | None,
    *,
    mark_inflight: bool = False,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Validate a claim while the caller holds this window's guard."""
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claims = _session_claims(state, binding, create=False)
        claim = claims.get(window_key(window))
        if changed:
            atomic_write_json(CLAIMS_FILE, state)
        if claim_token is not None:
            if len(claim_token) > MAX_CLAIM_TOKEN_LENGTH:
                raise ValueError(
                    f"claim_token must contain at most {MAX_CLAIM_TOKEN_LENGTH} characters"
                )
            if claim is None or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            ):
                raise ValueError("claim_token is invalid, expired, or belongs to another window")
            if owner_thread_id != claim.get("owner_thread_id"):
                raise RuntimeError("claim_token belongs to another computer-use agent")
        if claim is not None:
            if owner_thread_id != claim.get("owner_thread_id"):
                raise RuntimeError("window is actively claimed by another computer-use agent")
            if claim_token is None:
                raise ValueError("claim_token is required while this window has an active claim")
        if claim is not None:
            claim["expires_at"] = current_time + int(claim["lease_seconds"])
            if mark_inflight:
                claim["inflight_until"] = current_time + MAX_INFLIGHT_SECONDS
            atomic_write_json(CLAIMS_FILE, state)
        return claim.copy() if claim else None


def finish_window_access(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str,
    claim_token: str,
    *,
    renew: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Finish an in-flight operation while the caller still holds its window guard."""
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(window_key(window))
        if (
            claim is None
            or claim.get("owner_thread_id") != owner_thread_id
            or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            )
        ):
            raise RuntimeError("window claim changed while the claimed operation was running")
        claim.pop("inflight_until", None)
        if renew:
            claim["expires_at"] = current_time + int(claim["lease_seconds"])
        atomic_write_json(CLAIMS_FILE, state)
        return _public_claim(claim, include_token=True)


def renew_owned_claim(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str,
    *,
    mark_inflight: bool = False,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Renew the current owner's claim after a coordinate lease authenticated it."""
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(window_key(window))
        if claim is None:
            if changed:
                atomic_write_json(CLAIMS_FILE, state)
            return None
        if claim.get("owner_thread_id") != owner_thread_id:
            raise RuntimeError("coordinate window is actively claimed by another agent")
        claim["expires_at"] = current_time + int(claim["lease_seconds"])
        if mark_inflight:
            claim["inflight_until"] = current_time + MAX_INFLIGHT_SECONDS
        atomic_write_json(CLAIMS_FILE, state)
        return _public_claim(claim, include_token=True)


def claim_is_live(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str,
    claim_token: str,
    *,
    now: float | None = None,
) -> bool:
    current_time = time.time() if now is None else now
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(window_key(window))
        if changed:
            atomic_write_json(CLAIMS_FILE, state)
        return bool(
            claim
            and claim.get("owner_thread_id") == owner_thread_id
            and secrets.compare_digest(str(claim.get("claim_token") or ""), claim_token)
        )
