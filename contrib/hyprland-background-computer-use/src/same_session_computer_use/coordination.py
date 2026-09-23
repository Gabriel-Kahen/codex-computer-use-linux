import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import time
from contextlib import ExitStack
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
MAX_OWNER_LENGTH = 200
MAX_OWNER_BYTES = 512
MAX_CLAIM_TOKEN_LENGTH = 256
MAX_INFLIGHT_SECONDS = 300
MAX_STATE_BYTES = 1_048_576
PROTOCOL_VERSION = 2

def binding_key(binding: dict[str, Any]) -> str:
    encoded = json.dumps(
        binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _session_identity(binding: dict[str, Any]) -> dict[str, Any]:
    instance = binding.get("hyprland_instance")
    display = binding.get("wayland_display")
    uid = binding.get("uid")
    if (
        not isinstance(instance, str)
        or not instance
        or not isinstance(display, str)
        or not display
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
    ):
        raise RuntimeError("cannot establish the canonical Hyprland session identity")
    return {
        "backend": "hyprland",
        "uid": uid,
        "attributes": {
            "hyprland_instance": instance,
            "wayland_display": display,
        },
    }


def session_key(binding: dict[str, Any]) -> str:
    return binding_key(_session_identity(binding))


def _process_identity(window: dict[str, Any]) -> dict[str, int]:
    pid = window.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("selected window lacks a valid process identifier")
    start_time = window.get("process_start_time")
    if start_time is None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            start_time = int(fields[19])
        except (OSError, IndexError, ValueError) as exc:
            raise RuntimeError("cannot establish the selected window process identity") from exc
    if isinstance(start_time, bool) or not isinstance(start_time, int):
        raise RuntimeError("selected window has an invalid process start time")
    if start_time <= 0:
        raise RuntimeError("selected window has an invalid process start time")
    return {"pid": pid, "start_time": start_time}


def _window_identity(window: dict[str, Any]) -> dict[str, Any]:
    address = str(window.get("address") or "").lower()
    try:
        address = f"0x{int(address, 16):x}"
    except ValueError as exc:
        raise RuntimeError("selected window has no canonical compositor address") from exc
    return {
        "backend": "hyprland",
        "id": address,
        "process": _process_identity(window),
    }


def protocol_window_key(binding: dict[str, Any], window: dict[str, Any]) -> str:
    return binding_key(
        {
            "session": _session_identity(binding),
            "window": _window_identity(window),
        }
    )


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
def _window_guards(
    binding: dict[str, Any], legacy_key: str, canonical_key: str
):
    paths = {
        WINDOW_LOCK_DIR / f"{_lock_key(binding, legacy_key)}.lock",
        WINDOW_LOCK_DIR / f"{canonical_key}.lock",
    }
    with ExitStack() as stack:
        for path in sorted(paths):
            stack.enter_context(file_guard(path))
        yield


@contextmanager
def window_guard(binding: dict[str, Any], window: dict[str, Any]):
    """Serialize observe/mutate transactions for one compositor window."""
    with _window_guards(
        binding, window_lock_key(window), protocol_window_key(binding, window)
    ):
        yield


@contextmanager
def global_input_guard():
    """Serialize XWayland, physical-seat, and fallback transactions."""
    with file_guard(GLOBAL_INPUT_LOCK_FILE):
        yield


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True).encode()
    if len(encoded) > MAX_STATE_BYTES:
        raise RuntimeError(f"window claim state exceeds {MAX_STATE_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = os.lstat(path.parent)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise RuntimeError(
            "coordination directory must be owned by the current user and not be a symlink"
        )
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
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


def _empty_state() -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION, "sessions": {}}


def _load_state() -> dict[str, Any]:
    try:
        descriptor = os.open(
            CLAIMS_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise RuntimeError(f"window claim state is unreadable: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise RuntimeError(
                "window claim state must be a bounded private regular file owned by the current user"
            )
        with os.fdopen(descriptor) as handle:
            descriptor = -1
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"window claim state is unreadable: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
        raise RuntimeError("window claim state has an unsupported format")
    if state.get("version") == 1:
        for session in state["sessions"].values():
            claims = session.get("claims")
            if not isinstance(claims, dict):
                raise RuntimeError("legacy window claim state is malformed")
            for claim in claims.values():
                required = {"owner_thread_id", "claim_token", "claimed_at", "expires_at", "lease_seconds", "window"}
                deadline = claim.get("expires_at") if isinstance(claim, dict) else None
                inflight = claim.get("inflight_until") if isinstance(claim, dict) else None
                claimed_at = claim.get("claimed_at") if isinstance(claim, dict) else None
                lease = claim.get("lease_seconds") if isinstance(claim, dict) else None
                if (
                    not isinstance(claim, dict)
                    or not required <= set(claim)
                    or any(
                        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in (claimed_at, deadline)
                    )
                    or isinstance(lease, bool)
                    or not isinstance(lease, int)
                    or not MIN_LEASE_SECONDS <= lease <= MAX_LEASE_SECONDS
                    or inflight is not None
                    and (isinstance(inflight, bool) or not isinstance(inflight, (int, float)) or not math.isfinite(inflight))
                ):
                    raise RuntimeError("legacy window claim state is malformed")
                if max(deadline, inflight or 0) > time.time():
                    raise RuntimeError(
                        "legacy version-1 claims are still active; release them with the older "
                        "companion or wait for expiry before retrying the version-2 upgrade"
                    )
        return _empty_state()
    if state.get("version") != PROTOCOL_VERSION:
        raise RuntimeError("window claim state has an unsupported format")
    _validate_state(state)
    return state


def _session_claims(
    state: dict[str, Any], binding: dict[str, Any], *, create: bool
) -> dict[str, dict[str, Any]]:
    key = session_key(binding)
    identity = _session_identity(binding)
    session = state["sessions"].get(key)
    if session is None:
        if not create:
            return {}
        if len(state["sessions"]) >= 32:
            raise RuntimeError("coordination session limit reached")
        session = {
            "identity": identity,
            "next_fencing_token": 1,
            "claims": {},
        }
        state["sessions"][key] = session
    if session.get("identity") != identity or not isinstance(session.get("claims"), dict):
        raise RuntimeError("window claim state does not match the active display and Hyprland instance")
    return session["claims"]


def _prune_expired(state: dict[str, Any], now: int) -> bool:
    changed = False
    for key, session in list(state["sessions"].items()):
        claims = session.get("claims")
        if not isinstance(claims, dict):
            raise RuntimeError("window claim state is malformed")
        for claimed_window, claim in list(claims.items()):
            live_until = max(
                int(claim.get("expires_at_ms") or 0),
                int(claim.get("inflight_until_ms") or 0),
            )
            if live_until <= now:
                del claims[claimed_window]
                changed = True
    return changed


def _window_summary(window: dict[str, Any]) -> dict[str, Any]:
    limits = {"address": 64, "capture_id": 64, "class": 80, "title": 160}
    return {
        key: "".join(
            " " if ord(character) < 32 or 127 <= ord(character) <= 159 else character
            for character in str(window[key])[:limit]
        )
        for key, limit in limits.items()
        if window.get(key) is not None
    }


def _public_claim(claim: dict[str, Any], *, include_token: bool) -> dict[str, Any]:
    owner = claim.get("owner_thread_id")
    if (
        not isinstance(owner, str)
        or not owner
        or len(owner) > MAX_OWNER_LENGTH
        or len(owner.encode()) > MAX_OWNER_BYTES
    ):
        raise RuntimeError("window claim state contains an invalid owner")
    result = {
        "window": dict((claim.get("window") or {}).get("summary") or {}),
        "owner_thread_id": owner,
        "fencing_token": int(claim["fencing_token"]),
        "claimed_at": int(claim["claimed_at_ms"]) / 1000,
        "expires_at": int(claim["expires_at_ms"]) / 1000,
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


def _validate_state(state: dict[str, Any]) -> None:
    if set(state) != {"version", "sessions"} or len(state["sessions"]) > 32:
        raise RuntimeError("coordination state contains too many sessions")
    for key, session in state["sessions"].items():
        identity = session.get("identity")
        claims = session.get("claims")
        next_token = session.get("next_fencing_token")
        if (
            set(session) != {"identity", "next_fencing_token", "claims"}
            or not isinstance(identity, dict)
            or set(identity) != {"backend", "uid", "attributes"}
            or identity.get("backend")
            not in {"cosmic", "gnome", "hyprland", "i3", "niri", "plasma", "x11"}
            or isinstance(identity.get("uid"), bool)
            or not isinstance(identity.get("uid"), int)
            or not 0 <= identity["uid"] <= 2**32 - 1
            or not isinstance(identity.get("attributes"), dict)
            or not 1 <= len(identity["attributes"]) <= 16
            or key != binding_key(identity)
            or not isinstance(claims, dict)
            or len(claims) > MAX_ACTIVE_CLAIMS
            or isinstance(next_token, bool)
            or not isinstance(next_token, int)
            or not 0 < next_token <= 2**64 - 1
        ):
            raise RuntimeError("window claim state contains an invalid session")
        for attribute_key, attribute in identity["attributes"].items():
            if (
                not isinstance(attribute_key, str)
                or not attribute_key
                or len(attribute_key) > 64
                or isinstance(attribute, bool)
                or not isinstance(attribute, (str, int))
                or (isinstance(attribute, str) and len(attribute.encode()) > 512)
                or (isinstance(attribute, int) and not 0 <= attribute <= 2**64 - 1)
            ):
                raise RuntimeError("window claim state contains an invalid session identity")
        issued: set[int] = set()
        for window_key_value, claim in claims.items():
            window = claim.get("window") or {}
            identity_value = window.get("identity")
            fence = claim.get("fencing_token")
            lease = claim.get("lease_seconds")
            renewed = claim.get("renewed_at_ms")
            expires = claim.get("expires_at_ms")
            claimed = claim.get("claimed_at_ms")
            inflight = claim.get("inflight_until_ms")
            owner = claim.get("owner_thread_id")
            token = claim.get("claim_token")
            allowed_claim_keys = {
                "owner_thread_id",
                "claim_token",
                "fencing_token",
                "claimed_at_ms",
                "renewed_at_ms",
                "expires_at_ms",
                "lease_seconds",
                "window",
            }
            allowed_claim_keys.update(
                key for key in ("owner_process", "inflight_until_ms") if key in claim
            )
            allowed_window_keys = {"identity"} | ({"summary"} if "summary" in window else set())
            if (
                not isinstance(claim, dict)
                or set(claim) != allowed_claim_keys
                or not isinstance(window, dict)
                or set(window) != allowed_window_keys
                or not isinstance(identity_value, dict)
                or set(identity_value)
                != {"backend", "id"}
                | ({"process"} if "process" in identity_value else set())
                or window_key_value
                != binding_key({"session": identity, "window": identity_value})
                or identity_value.get("backend") != identity.get("backend")
                or not isinstance(identity_value.get("id"), str)
                or not identity_value["id"]
                or len(identity_value["id"]) > 256
                or isinstance(fence, bool)
                or not isinstance(fence, int)
                or not 0 < fence <= 2**64 - 1
                or fence in issued
                or isinstance(lease, bool)
                or not isinstance(lease, int)
                or not MIN_LEASE_SECONDS <= lease <= MAX_LEASE_SECONDS
                or not isinstance(renewed, int)
                or expires != renewed + lease * 1000
                or isinstance(claimed, bool)
                or not isinstance(claimed, int)
                or claimed <= 0
                or renewed < claimed
                or (
                    inflight is not None
                    and (
                        not isinstance(inflight, int)
                        or inflight < renewed
                        or inflight > expires + MAX_INFLIGHT_SECONDS * 1000
                    )
                )
                or not isinstance(owner, str)
                or not owner
                or len(owner) > MAX_OWNER_LENGTH
                or len(owner.encode()) > MAX_OWNER_BYTES
                or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in owner)
                or not isinstance(token, str)
                or not token
                or len(token) > MAX_CLAIM_TOKEN_LENGTH
                or len(token.encode()) > MAX_CLAIM_TOKEN_LENGTH
                or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in token)
            ):
                raise RuntimeError("window claim state contains an invalid claim")
            for process in (claim.get("owner_process"), identity_value.get("process")):
                if process is not None and (
                    not isinstance(process, dict)
                    or set(process) != {"pid", "start_time"}
                    or not all(
                        isinstance(value, int) and not isinstance(value, bool) and value > 0
                        for value in process.values()
                    )
                ):
                    raise RuntimeError("window claim state contains an invalid process identity")
            summary = window.get("summary", {})
            if (
                not isinstance(summary, dict)
                or len(summary) > 8
                or any(
                    not isinstance(name, str)
                    or not name
                    or len(name) > 64
                    or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in name)
                    or not isinstance(value, str)
                    or len(value) > 256
                    or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
                    for name, value in summary.items()
                )
            ):
                raise RuntimeError("window claim state contains an invalid window summary")
            issued.add(fence)
        if issued and next_token <= max(issued):
            raise RuntimeError("window claim fencing sequence is not monotonic")


def claim_window(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    claim_token: str | None = None,
    *,
    reservation_owner: Callable[[], str | None] | None = None,
    after_claim: Callable[[dict[str, Any]], None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(owner_thread_id, str)
        or not owner_thread_id.strip()
        or len(owner_thread_id) > MAX_OWNER_LENGTH
        or len(owner_thread_id.encode()) > MAX_OWNER_BYTES
        or any(
            ord(character) < 32 or 127 <= ord(character) <= 159
            for character in owner_thread_id
        )
    ):
        raise ValueError(f"owner_thread_id must contain 1..{MAX_OWNER_LENGTH} characters")
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )
    if claim_token is not None and (
        not isinstance(claim_token, str)
        or not claim_token
        or len(claim_token) > MAX_CLAIM_TOKEN_LENGTH
    ):
        raise ValueError(
            f"claim_token must contain 1..{MAX_CLAIM_TOKEN_LENGTH} characters"
        )
    current_time = time.time() if now is None else now
    current_time_ms = int(current_time * 1000)
    key = protocol_window_key(binding, window)
    with window_guard(binding, window):
        reserved_by = reservation_owner() if reservation_owner else None
        if reserved_by is not None and reserved_by != owner_thread_id:
            raise RuntimeError("window is reserved by another agent's active coordinate lease")
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time_ms)
            claims = _session_claims(state, binding, create=True)
            existing = claims.get(key)
            if existing and existing.get("owner_thread_id") != owner_thread_id:
                raise RuntimeError(
                    "window is claimed by another computer-use agent until "
                    f"{int(existing['expires_at_ms']) / 1000}"
                )
            if existing is not None and (
                claim_token is None
                or not secrets.compare_digest(
                    str(existing.get("claim_token") or ""), claim_token
                )
            ):
                raise ValueError(
                    "the current claim_token is required to renew this window claim"
                )
            if existing is None and claim_token is not None:
                raise ValueError(
                    "claim_token is invalid, expired, or belongs to another window"
                )
            if existing is None and len(claims) >= MAX_ACTIVE_CLAIMS:
                raise RuntimeError(
                    f"active window claim limit ({MAX_ACTIVE_CLAIMS}) reached for this session"
                )
            renewed = existing is not None
            session = state["sessions"][session_key(binding)]
            if existing is None:
                fencing_token = int(session["next_fencing_token"])
                if fencing_token >= 2**64 - 1:
                    raise RuntimeError("window claim fencing token is exhausted")
                session["next_fencing_token"] = fencing_token + 1
                claim = {
                    "claim_token": secrets.token_urlsafe(24),
                    "owner_thread_id": owner_thread_id,
                    "fencing_token": fencing_token,
                    "claimed_at_ms": current_time_ms,
                    "window": {
                        "identity": _window_identity(window),
                        "summary": _window_summary(window),
                    },
                }
            else:
                claim = existing
            claim.update(
                {
                    "renewed_at_ms": current_time_ms,
                    "lease_seconds": lease_seconds,
                    "expires_at_ms": current_time_ms + lease_seconds * 1000,
                }
            )
            if after_claim:
                claim["inflight_until_ms"] = (
                    current_time_ms + MAX_INFLIGHT_SECONDS * 1000
                )
            claims[key] = claim
            _validate_state(state)
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
                        claim.pop("inflight_until_ms", None)
                        atomic_write_json(CLAIMS_FILE, state)
                raise
            completed_at = time.time() if now is None else now
            completed_at_ms = int(completed_at * 1000)
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
                claim.pop("inflight_until_ms", None)
                claim["renewed_at_ms"] = completed_at_ms
                claim["expires_at_ms"] = completed_at_ms + lease_seconds * 1000
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
    current_time_ms = int(current_time * 1000)
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time_ms)
        claims = _session_claims(state, binding, create=False)
        found = _find_claim_by_token(claims, claim_token)
        if found is None:
            if changed:
                atomic_write_json(CLAIMS_FILE, state)
            return {"released": False}
        claim, key = found
        if claim.get("owner_thread_id") != owner_thread_id:
            raise RuntimeError("only the agent that owns a live window claim may release it")

    summary = dict((claim.get("window") or {}).get("summary") or {})
    identity = dict((claim.get("window") or {}).get("identity") or {})
    process = identity.get("process") or {}
    lock_window = {
        **summary,
        "address": identity.get("id"),
        "pid": process.get("pid"),
    }
    with _window_guards(
        binding, window_lock_key(lock_window), key
    ):
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time_ms)
            claims = _session_claims(state, binding, create=False)
            claim = claims.get(key)
            if claim is None or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            ):
                atomic_write_json(CLAIMS_FILE, state)
                return {"released": False}
            if claim.get("owner_thread_id") != owner_thread_id:
                raise RuntimeError("only the agent that owns a live window claim may release it")
            claim_snapshot = _public_claim(claim, include_token=False)
        if before_release:
            before_release(claim_snapshot)
        with file_guard(CLAIMS_LOCK_FILE):
            state = _load_state()
            _prune_expired(state, current_time_ms)
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
            _prune_expired(state, current_time_ms)
            atomic_write_json(CLAIMS_FILE, state)
            return {
                "released": True,
                "window": dict((claim.get("window") or {}).get("summary") or {}),
            }


def list_claims(binding: dict[str, Any], *, now: float | None = None) -> list[dict[str, Any]]:
    current_time = int((time.time() if now is None else now) * 1000)
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
    current_time = int((time.time() if now is None else now) * 1000)
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claims = _session_claims(state, binding, create=False)
        claim = claims.get(protocol_window_key(binding, window))
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
            claim["renewed_at_ms"] = current_time
            claim["expires_at_ms"] = (
                current_time + int(claim["lease_seconds"]) * 1000
            )
            if mark_inflight:
                claim["inflight_until_ms"] = (
                    current_time + MAX_INFLIGHT_SECONDS * 1000
                )
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
    current_time = int((time.time() if now is None else now) * 1000)
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(
            protocol_window_key(binding, window)
        )
        if (
            claim is None
            or claim.get("owner_thread_id") != owner_thread_id
            or not secrets.compare_digest(
                str(claim.get("claim_token") or ""), claim_token
            )
        ):
            raise RuntimeError("window claim changed while the claimed operation was running")
        claim.pop("inflight_until_ms", None)
        if renew:
            claim["renewed_at_ms"] = current_time
            claim["expires_at_ms"] = (
                current_time + int(claim["lease_seconds"]) * 1000
            )
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
    current_time = int((time.time() if now is None else now) * 1000)
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(
            protocol_window_key(binding, window)
        )
        if claim is None:
            if changed:
                atomic_write_json(CLAIMS_FILE, state)
            return None
        if claim.get("owner_thread_id") != owner_thread_id:
            raise RuntimeError("coordinate window is actively claimed by another agent")
        claim["renewed_at_ms"] = current_time
        claim["expires_at_ms"] = current_time + int(claim["lease_seconds"]) * 1000
        if mark_inflight:
            claim["inflight_until_ms"] = current_time + MAX_INFLIGHT_SECONDS * 1000
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
    current_time = int((time.time() if now is None else now) * 1000)
    with file_guard(CLAIMS_LOCK_FILE):
        state = _load_state()
        changed = _prune_expired(state, current_time)
        claim = _session_claims(state, binding, create=False).get(
            protocol_window_key(binding, window)
        )
        if changed:
            atomic_write_json(CLAIMS_FILE, state)
        return bool(
            claim
            and claim.get("owner_thread_id") == owner_thread_id
            and secrets.compare_digest(str(claim.get("claim_token") or ""), claim_token)
        )
