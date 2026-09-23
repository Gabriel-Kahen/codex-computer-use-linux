import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from enum import Enum
from typing import Any

from . import coordination
from . import kwin


class ClaimCleanupResult(Enum):
    """Describes whether implicit-claim cleanup removed or safely skipped state."""

    RELEASED = "released"
    ALREADY_ABSENT = "already_absent"
    BLOCKED = "blocked"


def claim_window(
    window: dict[str, Any],
    owner_id: str,
    lease_seconds: int,
    claim_token: str | None = None,
) -> dict[str, Any]:
    lease_seconds = coordination._validate_lease_seconds(lease_seconds)
    owner_id = coordination.require_thread_id(owner_id)
    window = coordination.window_for_model(window)
    session = coordination.current_session_identity()
    claims_dir = coordination._session_claims_dir(session)
    key = coordination._claim_key(window["id"])
    if claim_token is not None and coordination._token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = coordination._claim_paths(key, claims_dir)
    with kwin.file_guard(claims_dir / "registry.lock"):
        with kwin.file_guard(lock):
            record = coordination.read_private_json(path)
            status = coordination._record_status(record, session, time.time()) if record else "missing"
            if status in {"active", "reserved"} and record is not None and not coordination._same_owner(record, owner_id):
                raise RuntimeError("the selected window has an active claim owned by another agent")
            if status in {"active", "reserved"} and record is not None:
                if claim_token is None:
                    raise ValueError("an active window claim requires its claim_token")
                if not coordination._same_token(record, claim_token):
                    raise ValueError("claim_token does not match the selected window claim")
                coordination._upgrade_record_binding(record)
                renewed = True
                record["expires_at"] = max(int(record["expires_at"]), int(time.time()) + lease_seconds)
                record["lease_seconds"] = max(int(record.get("lease_seconds") or 0), lease_seconds)
                record["owner"]["process"] = coordination.current_process_identity()
                record["implicit"] = False
                record["window"] = window
            else:
                if claim_token is not None:
                    raise RuntimeError("claim_token does not match an active window claim")
                renewed = False
                path.unlink(missing_ok=True)
                coordination._enforce_claim_capacity(session, path)
                record = coordination._new_claim(window, owner_id, lease_seconds, session, implicit=False)
            coordination.write_private_json(path, record)
            return coordination._public_claim(record, include_token=True, renewed=renewed)


@contextmanager
def window_action(
    window_id: str,
    owner_id: str,
    claim_token: str | None = None,
) -> Iterator[dict[str, Any] | None]:
    window_id = coordination.require_window_id(window_id)
    owner_id = coordination.require_thread_id(owner_id)
    session = coordination.current_session_identity()
    claims_dir = coordination._session_claims_dir(session)
    key = coordination._claim_key(window_id)
    if claim_token is not None and coordination._token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = coordination._claim_paths(key, claims_dir)
    with kwin.file_guard(lock):
        record = coordination.read_private_json(path)
        if record is None and claim_token is None:
            yield None
            return
        status = coordination._record_status(record, session, time.time()) if record else "missing"
        if status not in {"active", "reserved"}:
            path.unlink(missing_ok=True)
            if claim_token is not None:
                raise RuntimeError("claim_token does not match an active window claim")
            yield None
            return
        assert record is not None
        if not coordination._same_owner(record, owner_id):
            raise RuntimeError("the selected window has an active claim owned by another agent")
        if claim_token is None:
            raise ValueError("an active window claim requires its claim_token")
        if not coordination._same_token(record, claim_token):
            raise ValueError("claim_token does not match the selected window claim")
        coordination._upgrade_record_binding(record)
        record["owner"]["process"] = coordination.current_process_identity()
        coordination.write_private_json(path, record)
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
    lease_seconds = coordination._validate_lease_seconds(lease_seconds)
    owner_id = coordination.require_thread_id(owner_id)
    window = coordination.window_for_model(window)
    session = coordination.current_session_identity()
    if session != expected_session:
        raise RuntimeError("KWin session identity changed before the focus claim could be acquired")
    claims_dir = coordination._session_claims_dir(session)
    key = coordination._claim_key(window["id"])
    if claim_token is not None and coordination._token_key(claim_token) != key:
        raise ValueError("claim_token does not belong to the selected window")
    path, lock = coordination._claim_paths(key, claims_dir)
    with kwin.file_guard(claims_dir / "registry.lock"):
        with kwin.file_guard(lock):
            record = coordination.read_private_json(path)
            previous = deepcopy(record)
            status = coordination._record_status(record, session, time.time()) if record else "missing"
            if status in {"active", "reserved"} and record is not None and not coordination._same_owner(record, owner_id):
                raise RuntimeError("the selected window has an active claim owned by another agent")
            if status in {"active", "reserved"} and record is not None:
                if claim_token is None:
                    raise ValueError("an active window claim requires its claim_token")
                if not coordination._same_token(record, claim_token):
                    raise ValueError("claim_token does not match the selected window claim")
                coordination._upgrade_record_binding(record)
                record["expires_at"] = max(int(record["expires_at"]), int(time.time()) + lease_seconds)
                record["lease_seconds"] = max(int(record.get("lease_seconds") or 0), lease_seconds)
                record["owner"]["process"] = coordination.current_process_identity()
                record["window"] = window
            else:
                if claim_token is not None:
                    raise RuntimeError("claim_token does not match an active window claim")
                coordination._enforce_claim_capacity(session, path)
                record = coordination._new_claim(window, owner_id, lease_seconds, session, implicit=True)
            try:
                coordination.write_private_json(path, record)
                yield record
            except BaseException:
                try:
                    journal_reserves_claim = coordination._focus_reserves(record)
                except Exception:
                    journal_reserves_claim = True
                if not journal_reserves_claim:
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        coordination.write_private_json(path, previous)
                raise


def ensure_focus_claim(
    window: dict[str, Any],
    owner_id: str,
    claim_token: str | None,
    lease_seconds: int,
) -> dict[str, Any]:
    session = coordination.current_session_identity()
    with focus_claim_transaction(window, owner_id, claim_token, lease_seconds, session) as record:
        return record


def release_window_claim(claim_token: str, owner_id: str) -> dict[str, Any]:
    key = coordination._token_key(claim_token)
    owner_id = coordination.require_thread_id(owner_id)
    session = coordination.current_session_identity()
    claims_dir = coordination._session_claims_dir(session)
    path, lock = coordination._claim_paths(key, claims_dir)
    with kwin.file_guard(lock):
        record = coordination.read_private_json(path)
        if record is None:
            return {"released": False, "message": "window claim no longer exists"}
        coordination._record_binding(record)
        if not coordination._same_token(record, claim_token):
            raise ValueError("claim_token does not match the window claim")
        if coordination._focus_reserves(record):
            raise RuntimeError("cannot release a window claim reserved by unfinished focus restoration")
        status = coordination._record_status(record, session, time.time())
        if status == "reserved":
            raise RuntimeError("cannot release a window claim reserved by unfinished focus restoration")
        if status == "active" and not coordination._same_owner(record, owner_id):
            raise RuntimeError("cannot release a live window claim owned by another agent")
        path.unlink(missing_ok=True)
        return {
            "released": True,
            "window": coordination.window_summary_for_model(record.get("window") or {}),
            "recovery_reason": None if status == "active" else status,
        }


def discard_bound_claim(claim_token: str, window_id: str, owner_id: str) -> ClaimCleanupResult:
    key = coordination._token_key(claim_token)
    if key != coordination._claim_key(window_id):
        return ClaimCleanupResult.BLOCKED
    session = coordination.current_session_identity()
    claims_dir = coordination._session_claims_dir(session)
    path, lock = coordination._claim_paths(key, claims_dir)
    with kwin.file_guard(lock):
        record = coordination.read_private_json(path)
        if record is None:
            return ClaimCleanupResult.ALREADY_ABSENT
        coordination._record_binding(record)
        if (
            record.get("implicit") is not True
            or not coordination._same_token(record, claim_token)
            or not coordination._same_owner(record, owner_id)
        ):
            return ClaimCleanupResult.BLOCKED
        path.unlink(missing_ok=True)
        return ClaimCleanupResult.RELEASED
