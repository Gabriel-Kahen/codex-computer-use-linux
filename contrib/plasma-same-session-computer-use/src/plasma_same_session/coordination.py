import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

from . import kwin
from .coordination_state import CLAIM_TOKEN_PATTERN
from .coordination_state import MAX_CLAIM_TOKEN_CHARS
from .coordination_state import MAX_THREAD_ID_BYTES
from .coordination_state import MAX_THREAD_ID_CHARS
from .coordination_state import MAX_WINDOW_CLASS_BYTES
from .coordination_state import MAX_WINDOW_CLASS_CHARS
from .coordination_state import MAX_WINDOW_ID_BYTES
from .coordination_state import MAX_WINDOW_ID_CHARS
from .coordination_state import MAX_WINDOW_QUERY_BYTES
from .coordination_state import MAX_WINDOW_QUERY_CHARS
from .coordination_state import MAX_WINDOW_TITLE_BYTES
from .coordination_state import MAX_WINDOW_TITLE_CHARS
from .coordination_state import current_process_identity
from .coordination_state import current_session_identity
from .coordination_state import ensure_private_directory
from .coordination_state import legacy_owner_id
from .coordination_state import optional_desktop
from .coordination_state import optional_pointer
from .coordination_state import parse_thread_id
from .coordination_state import process_identity
from .coordination_state import process_is_alive
from .coordination_state import read_private_json
from .coordination_state import require_claim_token
from .coordination_state import require_thread_id
from .coordination_state import require_window_id
from .coordination_state import require_window_query
from .coordination_state import serialized_size
from .coordination_state import truncate_json_text as _truncate_json_text
from .coordination_state import window_for_model
from .coordination_state import window_summary_for_model
from .coordination_state import write_private_json


CLAIMS_DIR = kwin.STATE_DIR / "window-claims"
FOCUS_LEASE_FILE = kwin.STATE_DIR / "focus-lease.json"
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
MAX_ACTIVE_CLAIMS = 128
MAX_CLAIMS_PER_PAGE = 20
MAX_CLAIM_LIST_BYTES = 2 * 1024
MAX_CLAIM_RESULT_BYTES = 2 * 1024
MAX_TIMESTAMP = 2**63 - 1


def _claim_key(window_id: str) -> str:
    return hashlib.sha256(window_id.encode()).hexdigest()


def _session_claims_dir(session: dict[str, Any]) -> Path:
    if not isinstance(session, dict):
        raise RuntimeError("KWin session identity is invalid")
    encoded = json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return CLAIMS_DIR / hashlib.sha256(encoded).hexdigest()


def _claim_paths(key: str, claims_dir: Path) -> tuple[Path, Path]:
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise ValueError("claim token has an invalid window key")
    return claims_dir / f"{key}.json", claims_dir / f"{key}.lock"


def _token_key(token: str) -> str:
    token = require_claim_token(token)
    key, secret = token.split(".", 1)
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise ValueError("claim token has an invalid window key")
    return key


def _record_binding(record: dict[str, Any]) -> dict[str, Any]:
    binding = record.get("binding")
    owner = record.get("owner")
    window = record.get("window")
    if not isinstance(owner, dict) or not isinstance(window, dict):
        raise RuntimeError("window claim binding is invalid")
    expected = {
        "window_id": window.get("id"),
        "owner_thread_id": owner.get("thread_id"),
        "session_identity": record.get("session_identity"),
        "claim_token": record.get("claim_token"),
    }
    if binding is None and record.get("version") == 1:
        binding = expected
    elif binding != expected:
        raise RuntimeError("window claim binding is invalid")
    require_window_id(binding.get("window_id"))
    require_thread_id(binding.get("owner_thread_id"))
    require_claim_token(binding.get("claim_token"))
    if not isinstance(binding.get("session_identity"), dict):
        raise RuntimeError("window claim binding is invalid")
    return binding


def _upgrade_record_binding(record: dict[str, Any]) -> None:
    binding = _record_binding(record)
    record["version"] = 2
    record["binding"] = binding


def _focus_binding(state: dict[str, Any]) -> dict[str, Any] | None:
    binding = state.get("binding")
    if binding is None:
        if state.get("version") != 1:
            raise RuntimeError("focus lease binding is invalid")
        claim = state.get("window_claim")
        owner = state.get("owner")
        target = state.get("target")
        if not isinstance(claim, dict) or not isinstance(owner, dict) or not isinstance(target, dict):
            return None
        binding = {
            "target_window_id": target.get("id"),
            "owner_thread_id": owner.get("thread_id"),
            "session_identity": state.get("session_identity"),
            "claim_token": claim.get("claim_token"),
        }
    else:
        claim = state.get("window_claim")
        owner = state.get("owner")
        target = state.get("target")
        if not isinstance(claim, dict) or not isinstance(owner, dict) or not isinstance(target, dict):
            raise RuntimeError("focus lease binding is invalid")
        expected = {
            "target_window_id": target.get("id"),
            "owner_thread_id": owner.get("thread_id"),
            "session_identity": state.get("session_identity"),
            "claim_token": claim.get("claim_token"),
        }
        if binding != expected:
            raise RuntimeError("focus lease binding is invalid")
    if not isinstance(binding, dict):
        raise RuntimeError("focus lease binding is invalid")
    if claim.get("window_id") != binding["target_window_id"]:
        raise RuntimeError("focus lease binding is invalid")
    require_window_id(binding.get("target_window_id"))
    require_thread_id(binding.get("owner_thread_id"))
    require_claim_token(binding.get("claim_token"))
    if not isinstance(binding.get("session_identity"), dict):
        raise RuntimeError("focus lease binding is invalid")
    return binding


def _focus_reserves(record: dict[str, Any]) -> bool:
    state = read_private_json(FOCUS_LEASE_FILE)
    if state is None:
        return False
    binding = _focus_binding(state)
    if binding is None:
        raise RuntimeError("unfinished focus restoration state cannot be verified")
    claim_binding = _record_binding(record)
    if binding.get("target_window_id") != claim_binding["window_id"]:
        return False
    expected = {
        "target_window_id": claim_binding["window_id"],
        "owner_thread_id": claim_binding["owner_thread_id"],
        "session_identity": claim_binding["session_identity"],
        "claim_token": claim_binding["claim_token"],
    }
    if binding != expected or state.get("phase") not in {"prepared", "active"}:
        raise RuntimeError("unfinished focus restoration state does not match the window claim")
    return True


def _record_status(record: dict[str, Any], session: dict[str, Any], now: float) -> str:
    binding = _record_binding(record)
    expires_at = _timestamp(record.get("expires_at"))
    if binding["session_identity"] != session:
        return "different-session"
    if now >= expires_at:
        return "reserved" if _focus_reserves(record) else "expired"
    if not process_is_alive(record["owner"].get("process")):
        return "reserved" if _focus_reserves(record) else "owner-exited"
    return "active"


def _same_owner(record: dict[str, Any], owner_id: str) -> bool:
    recorded = str((record.get("owner") or {}).get("thread_id") or "")
    return secrets.compare_digest(recorded, owner_id)


def _same_token(record: dict[str, Any], claim_token: str) -> bool:
    return secrets.compare_digest(str(record.get("claim_token") or ""), claim_token)


def _public_claim(record: dict[str, Any], *, include_token: bool, renewed: bool | None = None) -> dict[str, Any]:
    owner = record.get("owner") or {}
    claimed_at = _timestamp(record.get("created_at"))
    expires_at = _timestamp(record.get("expires_at"))
    lease_seconds = record.get("lease_seconds")
    _validate_lease_seconds(lease_seconds)
    value = {
        "window": window_summary_for_model(record.get("window") or {}),
        "owner_thread_id": require_thread_id(owner.get("thread_id")),
        "claimed_at": claimed_at,
        "expires_at": expires_at,
        "lease_seconds": lease_seconds,
    }
    if include_token:
        value["claim_token"] = record.get("claim_token")
    if renewed is not None:
        value["renewed"] = renewed
    if serialized_size(value) > MAX_CLAIM_RESULT_BYTES:
        raise RuntimeError("window claim result exceeds its serialized size limit")
    return value


def _new_claim(
    window: dict[str, Any],
    owner_id: str,
    lease_seconds: int,
    session: dict[str, Any],
    *,
    implicit: bool,
) -> dict[str, Any]:
    now = time.time()
    window = window_for_model(window)
    key = _claim_key(window["id"])
    token = f"{key}.{secrets.token_urlsafe(24)}"
    return {
        "version": 2,
        "claim_token": token,
        "window": window,
        "owner": {"thread_id": owner_id, "process": current_process_identity()},
        "session_identity": session,
        "binding": {
            "window_id": window["id"],
            "owner_thread_id": owner_id,
            "session_identity": session,
            "claim_token": token,
        },
        "implicit": implicit,
        "created_at": int(now),
        "expires_at": int(now) + lease_seconds,
        "lease_seconds": lease_seconds,
    }


def _validate_lease_seconds(value: Any) -> int:
    if type(value) is not int or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}")
    return value


def _timestamp(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP:
        raise RuntimeError("window claim timing metadata is invalid")
    return value


def _enforce_claim_capacity(session: dict[str, Any], target_path: Path) -> None:
    active = 0
    claims_dir = target_path.parent
    ensure_private_directory(claims_dir)
    for path in sorted(claims_dir.glob("*.json")):
        if path == target_path:
            continue
        with kwin.file_guard(path.with_suffix(".lock")):
            record = read_private_json(path)
            if record is None:
                continue
            status = _record_status(record, session, time.time())
            if status == "different-session":
                continue
            if status in {"active", "reserved"}:
                active += 1
                if active >= MAX_ACTIVE_CLAIMS:
                    raise RuntimeError(f"at most {MAX_ACTIVE_CLAIMS} live window claims are allowed")
            else:
                path.unlink(missing_ok=True)

def list_claims(_owner_id: str, offset: int, limit: int) -> dict[str, Any]:
    require_thread_id(_owner_id)
    if type(offset) is not int or not 0 <= offset <= MAX_ACTIVE_CLAIMS:
        raise ValueError(f"offset must be between 0 and {MAX_ACTIVE_CLAIMS}")
    if type(limit) is not int or not 1 <= limit <= MAX_CLAIMS_PER_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_CLAIMS_PER_PAGE}")
    session = current_session_identity()
    claims_dir = _session_claims_dir(session)
    ensure_private_directory(claims_dir)
    claims = []
    truncated = False
    for path in sorted(claims_dir.glob("*.json")):
        if len(claims) >= MAX_ACTIVE_CLAIMS:
            truncated = True
            break
        lock = path.with_suffix(".lock")
        with kwin.file_guard(lock):
            record = read_private_json(path)
            if record is None:
                continue
            status = _record_status(record, session, time.time())
            if status == "different-session":
                continue
            if status not in {"active", "reserved"}:
                path.unlink(missing_ok=True)
                continue
            claims.append(_public_claim(record, include_token=False))
    page: list[dict[str, Any]] = []
    for claim in claims[offset : offset + limit]:
        candidate = [*page, claim]
        end = offset + len(candidate)
        value = {
            "claims": candidate,
            "total": len(claims),
            "next_offset": end if end < len(claims) else None,
            "truncated": end < min(offset + limit, len(claims)),
            "registry_truncated": truncated,
        }
        if serialized_size(value) > MAX_CLAIM_LIST_BYTES:
            if not page:
                raise RuntimeError("a window claim exceeds the serialized list size limit")
            break
        page = candidate
    end = offset + len(page)
    value = {
        "claims": page,
        "total": len(claims),
        "next_offset": end if end < len(claims) else None,
        "truncated": end < min(offset + limit, len(claims)),
        "registry_truncated": truncated,
    }
    if serialized_size(value) > MAX_CLAIM_LIST_BYTES:
        raise RuntimeError("window claim list metadata exceeds its serialized size limit")
    return value

