import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import kwin


CLAIMS_DIR = kwin.STATE_DIR / "window-claims"
FOCUS_LEASE_FILE = kwin.STATE_DIR / "focus-lease.json"
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
MAX_THREAD_ID_CHARS = 200
MAX_THREAD_ID_BYTES = 512
MAX_ACTIVE_CLAIMS = 128
MAX_WINDOW_ID_CHARS = 80
MAX_WINDOW_ID_BYTES = 320
MAX_WINDOW_TITLE_CHARS = 160
MAX_WINDOW_TITLE_BYTES = 160
MAX_WINDOW_CLASS_CHARS = 96
MAX_WINDOW_CLASS_BYTES = 96
MAX_WINDOW_QUERY_CHARS = 256
MAX_WINDOW_QUERY_BYTES = 1024
MAX_CLAIM_TOKEN_CHARS = 160
MAX_CLAIMS_PER_PAGE = 20
MAX_CLAIM_LIST_BYTES = 2 * 1024
CLAIM_TOKEN_PATTERN = r"[0-9a-f]{64}\.[A-Za-z0-9_-]{32,64}"


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _truncate_json_text(value: Any, max_chars: int, max_bytes: int) -> str:
    text = str(value or "")[:max_chars]
    if serialized_size(text) - 2 <= max_bytes:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if serialized_size(text[:middle]) - 2 <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _bounded_input(value: Any, name: str, max_chars: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value or len(value) > max_chars or len(value.encode()) > max_bytes:
        raise ValueError(f"{name} exceeds its size limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def require_window_query(value: Any) -> str:
    query = _bounded_input(value, "window", MAX_WINDOW_QUERY_CHARS, MAX_WINDOW_QUERY_BYTES)
    if not query.strip():
        raise ValueError("window must not be blank")
    return query


def require_window_id(value: Any) -> str:
    return _bounded_input(value, "window id", MAX_WINDOW_ID_CHARS, MAX_WINDOW_ID_BYTES)


def require_claim_token(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_CLAIM_TOKEN_CHARS:
        raise ValueError("claim_token is invalid")
    if re.fullmatch(CLAIM_TOKEN_PATTERN, value) is None:
        raise ValueError("claim_token is invalid")
    return value


def window_for_model(window: dict[str, Any]) -> dict[str, Any]:
    window_id = require_window_id(window.get("id"))
    capture_id = require_window_id(window.get("capture_id"))
    return {
        "id": window_id,
        "capture_id": capture_id,
        "title": _truncate_json_text(window.get("title"), MAX_WINDOW_TITLE_CHARS, MAX_WINDOW_TITLE_BYTES),
        "class": _truncate_json_text(window.get("class"), MAX_WINDOW_CLASS_CHARS, MAX_WINDOW_CLASS_BYTES),
    }


def parse_thread_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("MCP _meta.threadId must be a string")
    thread_id = value.strip()
    if not thread_id or len(thread_id) > MAX_THREAD_ID_CHARS or len(thread_id.encode()) > MAX_THREAD_ID_BYTES:
        raise ValueError("MCP _meta.threadId exceeds its size limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in thread_id):
        raise ValueError("MCP _meta.threadId must not contain control characters")
    return thread_id


def require_thread_id(value: Any) -> str:
    thread_id = parse_thread_id(value)
    if thread_id is None:
        raise ValueError("this tool requires host-provided MCP _meta.threadId")
    return thread_id


def process_identity(pid: int | None = None) -> dict[str, Any]:
    process_id = os.getpid() if pid is None else pid
    try:
        fields = Path(f"/proc/{process_id}/stat").read_text().rsplit(")", 1)[1].split()
        state = fields[0]
        start_time = fields[19]
    except (IndexError, OSError):
        state = None
        start_time = None
    return {"pid": process_id, "start_time": start_time, "state": state}


def process_is_alive(identity: Any) -> bool:
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int:
        return False
    expected_start = identity.get("start_time")
    current = process_identity(identity["pid"])
    if current["start_time"] is None or current["state"] == "Z":
        return False
    return expected_start is not None and secrets.compare_digest(str(expected_start), str(current["start_time"]))


def current_process_identity() -> dict[str, Any]:
    identity = process_identity()
    if identity["start_time"] is None or identity["state"] == "Z":
        raise RuntimeError("the broker process identity could not be positively verified through /proc")
    return identity


def legacy_owner_id() -> str:
    identity = current_process_identity()
    return f"legacy-process:{identity['pid']}:{identity['start_time']}"


def current_session_identity() -> dict[str, Any]:
    identity = kwin.session_identity()
    if not identity.get("kwin_service_owner"):
        raise RuntimeError("KWin session ownership could not be positively identified")
    if not identity.get("session_id") and not identity.get("wayland_socket"):
        raise RuntimeError("the current Plasma login could not be positively identified")
    return identity


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"private state path is not a directory: {path}")
    if details.st_uid != os.getuid():
        raise RuntimeError(f"private state directory is not owned by the current user: {path}")
    path.chmod(0o700)


def read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError(f"private state path is not a regular file: {path}")
    if details.st_uid != os.getuid():
        raise RuntimeError(f"private state file is not owned by the current user: {path}")
    path.chmod(0o600)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"private state file does not contain an object: {path}")
    return value


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _claim_key(window_id: str) -> str:
    return hashlib.sha256(window_id.encode()).hexdigest()


def _claim_paths(key: str) -> tuple[Path, Path]:
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise ValueError("claim token has an invalid window key")
    return CLAIMS_DIR / f"{key}.json", CLAIMS_DIR / f"{key}.lock"


def _token_key(token: str) -> str:
    token = require_claim_token(token)
    key, secret = token.split(".", 1)
    _claim_paths(key)
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
    expires_at = record.get("expires_at")
    if type(expires_at) is not int or expires_at < 0:
        raise RuntimeError("window claim timing metadata is invalid")
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
    claimed_at = record.get("created_at")
    expires_at = record.get("expires_at")
    lease_seconds = record.get("lease_seconds")
    if type(claimed_at) is not int or claimed_at < 0 or type(expires_at) is not int or expires_at < 0:
        raise RuntimeError("window claim timing metadata is invalid")
    _validate_lease_seconds(lease_seconds)
    value = {
        "window": window_for_model(record.get("window") or {}),
        "owner_thread_id": require_thread_id(owner.get("thread_id")),
        "claimed_at": claimed_at,
        "expires_at": expires_at,
        "lease_seconds": lease_seconds,
    }
    if include_token:
        value["claim_token"] = record.get("claim_token")
    if renewed is not None:
        value["renewed"] = renewed
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


def _enforce_claim_capacity(session: dict[str, Any], target_path: Path) -> None:
    active = 0
    ensure_private_directory(CLAIMS_DIR)
    for path in sorted(CLAIMS_DIR.glob("*.json")):
        if path == target_path:
            continue
        with kwin.file_guard(path.with_suffix(".lock")):
            record = read_private_json(path)
            if record is None:
                continue
            if _record_status(record, session, time.time()) in {"active", "reserved"}:
                active += 1
                if active >= MAX_ACTIVE_CLAIMS:
                    raise RuntimeError(f"at most {MAX_ACTIVE_CLAIMS} live window claims are allowed")
            else:
                path.unlink(missing_ok=True)


def claim_window(
    window: dict[str, Any],
    owner_id: str,
    lease_seconds: int,
    claim_token: str | None = None,
) -> dict[str, Any]:
    lease_seconds = _validate_lease_seconds(lease_seconds)
    owner_id = require_thread_id(owner_id)
    window = window_for_model(window)
    session = current_session_identity()
    key = _claim_key(window["id"])
    if claim_token is not None and _token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = _claim_paths(key)
    with kwin.file_guard(CLAIMS_DIR / "registry.lock"):
        with kwin.file_guard(lock):
            record = read_private_json(path)
            status = _record_status(record, session, time.time()) if record else "missing"
            if status in {"active", "reserved"} and record is not None and not _same_owner(record, owner_id):
                raise RuntimeError("the selected window has an active claim owned by another agent")
            if status in {"active", "reserved"} and record is not None:
                if claim_token is None:
                    raise ValueError("an active window claim requires its claim_token")
                if not _same_token(record, claim_token):
                    raise ValueError("claim_token does not match the selected window claim")
                _upgrade_record_binding(record)
                renewed = True
                record["expires_at"] = max(int(record["expires_at"]), int(time.time()) + lease_seconds)
                record["lease_seconds"] = max(int(record.get("lease_seconds") or 0), lease_seconds)
                record["owner"]["process"] = current_process_identity()
                record["implicit"] = False
                record["window"] = window
            else:
                if claim_token is not None:
                    raise RuntimeError("claim_token does not match an active window claim")
                renewed = False
                path.unlink(missing_ok=True)
                _enforce_claim_capacity(session, path)
                record = _new_claim(window, owner_id, lease_seconds, session, implicit=False)
            write_private_json(path, record)
            return _public_claim(record, include_token=True, renewed=renewed)


@contextmanager
def window_action(
    window_id: str,
    owner_id: str,
    claim_token: str | None = None,
) -> Iterator[dict[str, Any] | None]:
    window_id = require_window_id(window_id)
    owner_id = require_thread_id(owner_id)
    key = _claim_key(window_id)
    if claim_token is not None and _token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = _claim_paths(key)
    with kwin.file_guard(lock):
        record = read_private_json(path)
        if record is None and claim_token is None:
            yield None
            return
        session = current_session_identity()
        status = _record_status(record, session, time.time()) if record else "missing"
        if status not in {"active", "reserved"}:
            path.unlink(missing_ok=True)
            if claim_token is not None:
                raise RuntimeError("claim_token does not match an active window claim")
            yield None
            return
        assert record is not None
        if not _same_owner(record, owner_id):
            raise RuntimeError("the selected window has an active claim owned by another agent")
        if claim_token is None:
            raise ValueError("an active window claim requires its claim_token")
        if not _same_token(record, claim_token):
            raise ValueError("claim_token does not match the selected window claim")
        _upgrade_record_binding(record)
        record["owner"]["process"] = current_process_identity()
        write_private_json(path, record)
        yield record


def authorize_window(window_id: str, owner_id: str, claim_token: str | None = None) -> dict[str, Any] | None:
    with window_action(window_id, owner_id, claim_token) as record:
        return record


@contextmanager
def focus_claim_transaction(
    window: dict[str, Any],
    owner_id: str,
    claim_token: str | None,
    lease_seconds: int,
    expected_session: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    lease_seconds = _validate_lease_seconds(lease_seconds)
    owner_id = require_thread_id(owner_id)
    window = window_for_model(window)
    session = current_session_identity()
    if session != expected_session:
        raise RuntimeError("KWin session identity changed before the focus claim could be acquired")
    key = _claim_key(window["id"])
    if claim_token is not None and _token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = _claim_paths(key)
    with kwin.file_guard(CLAIMS_DIR / "registry.lock"):
        with kwin.file_guard(lock):
            record = read_private_json(path)
            previous = deepcopy(record)
            status = _record_status(record, session, time.time()) if record else "missing"
            if status in {"active", "reserved"} and record is not None and not _same_owner(record, owner_id):
                raise RuntimeError("the selected window has an active claim owned by another agent")
            if status in {"active", "reserved"} and record is not None:
                if claim_token is None:
                    raise ValueError("an active window claim requires its claim_token")
                if not _same_token(record, claim_token):
                    raise ValueError("claim_token does not match the selected window claim")
                _upgrade_record_binding(record)
                record["expires_at"] = max(int(record["expires_at"]), int(time.time()) + lease_seconds)
                record["lease_seconds"] = max(int(record.get("lease_seconds") or 0), lease_seconds)
                record["owner"]["process"] = current_process_identity()
                record["window"] = window
            else:
                if claim_token is not None:
                    raise RuntimeError("claim_token does not match an active window claim")
                _enforce_claim_capacity(session, path)
                record = _new_claim(window, owner_id, lease_seconds, session, implicit=True)
            try:
                write_private_json(path, record)
                yield record
            except BaseException:
                try:
                    journal_reserves_claim = _focus_reserves(record)
                except Exception:
                    journal_reserves_claim = True
                if not journal_reserves_claim:
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_private_json(path, previous)
                raise


def ensure_focus_claim(
    window: dict[str, Any],
    owner_id: str,
    claim_token: str | None,
    lease_seconds: int,
) -> dict[str, Any]:
    session = current_session_identity()
    with focus_claim_transaction(window, owner_id, claim_token, lease_seconds, session) as record:
        return record


def release_window_claim(claim_token: str, owner_id: str) -> dict[str, Any]:
    key = _token_key(claim_token)
    owner_id = require_thread_id(owner_id)
    session = current_session_identity()
    path, lock = _claim_paths(key)
    with kwin.file_guard(lock):
        record = read_private_json(path)
        if record is None:
            return {"released": False, "message": "window claim no longer exists"}
        _record_binding(record)
        if not _same_token(record, claim_token):
            raise ValueError("claim_token does not match the window claim")
        if record.get("session_identity") == session and _focus_reserves(record):
            raise RuntimeError("cannot release a window claim reserved by unfinished focus restoration")
        status = _record_status(record, session, time.time())
        if status == "reserved":
            raise RuntimeError("cannot release a window claim reserved by unfinished focus restoration")
        if status == "active" and not _same_owner(record, owner_id):
            raise RuntimeError("cannot release a live window claim owned by another agent")
        path.unlink(missing_ok=True)
        return {
            "released": True,
            "window": record.get("window"),
            "recovery_reason": None if status == "active" else status,
        }


def discard_bound_claim(claim_token: str, window_id: str, owner_id: str) -> bool:
    key = _token_key(claim_token)
    if key != _claim_key(window_id):
        return False
    path, lock = _claim_paths(key)
    with kwin.file_guard(lock):
        record = read_private_json(path)
        if record is not None:
            _record_binding(record)
        if (
            record is None
            or record.get("implicit") is not True
            or not _same_token(record, claim_token)
            or not _same_owner(record, owner_id)
        ):
            return False
        path.unlink(missing_ok=True)
        return True


def list_claims(_owner_id: str, offset: int, limit: int) -> dict[str, Any]:
    require_thread_id(_owner_id)
    if type(offset) is not int or not 0 <= offset <= MAX_ACTIVE_CLAIMS:
        raise ValueError(f"offset must be between 0 and {MAX_ACTIVE_CLAIMS}")
    if type(limit) is not int or not 1 <= limit <= MAX_CLAIMS_PER_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_CLAIMS_PER_PAGE}")
    session = current_session_identity()
    ensure_private_directory(CLAIMS_DIR)
    claims = []
    truncated = False
    for path in sorted(CLAIMS_DIR.glob("*.json")):
        if len(claims) >= MAX_ACTIVE_CLAIMS:
            truncated = True
            break
        lock = path.with_suffix(".lock")
        with kwin.file_guard(lock):
            record = read_private_json(path)
            if record is None:
                continue
            if _record_status(record, session, time.time()) not in {"active", "reserved"}:
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
