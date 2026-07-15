import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable


DEFAULT_LEASE_SECONDS = 60
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
MAX_ACTIVE_CLAIMS = 20
MAX_OWNER_LENGTH = 128
MAX_CLAIM_TOKEN_LENGTH = 128
MAX_INFLIGHT_SECONDS = 300

_SESSION_LOCK_IDENTITY_KEYS = (
    "session_id",
    "display",
    "socket",
    "socket_device",
    "socket_inode",
)
_WINDOW_LOCK_IDENTITY_KEYS = ("xid", "pid", "process_start_time")


class WindowClaimStore:
    def __init__(
        self,
        state_dir: Path,
        session_fingerprint: dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = state_dir
        self.session_fingerprint = session_fingerprint
        self.clock = clock
        self.claims_file = state_dir / "window-claims.json"
        self.claims_lock = state_dir / "window-claims.lock"
        self.window_locks = state_dir / "window-locks"

    @contextlib.contextmanager
    def window_guard(self, window_identity: dict[str, Any]):
        self._prepare_directory(self.state_dir)
        self._prepare_directory(self.window_locks)
        encoded = self._canonical(
            {
                "session": self._lock_identity(
                    self.session_fingerprint,
                    _SESSION_LOCK_IDENTITY_KEYS,
                ),
                "window": self._lock_identity(
                    window_identity,
                    _WINDOW_LOCK_IDENTITY_KEYS,
                ),
            }
        ).encode()
        lock_path = self.window_locks / f"{hashlib.sha256(encoded).hexdigest()}.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def claim(
        self,
        owner_thread_id: str,
        window: dict[str, Any],
        window_identity: dict[str, Any],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        owner_thread_id = self._validate_owner(owner_thread_id)
        lease_seconds = self._validate_lease_seconds(lease_seconds)
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            matching = [claim for claim in claims if self._same_window(claim, window_identity)]
            foreign = next((claim for claim in matching if claim["owner_thread_id"] != owner_thread_id), None)
            if foreign:
                raise RuntimeError(
                    "window is claimed by another computer-use agent until "
                    f"{foreign['expires_at']:.3f}"
                )
            if matching:
                renewed = True
                record = matching[0]
                record.update(window=self._window_summary(window), lease_seconds=lease_seconds, expires_at=now + lease_seconds)
            else:
                renewed = False
                if len(claims) >= MAX_ACTIVE_CLAIMS:
                    raise RuntimeError(
                        f"at most {MAX_ACTIVE_CLAIMS} window claims may be active at once"
                    )
                record = {
                    "token": secrets.token_urlsafe(24),
                    "owner_thread_id": owner_thread_id,
                    "session_fingerprint": self.session_fingerprint,
                    "window": self._window_summary(window),
                    "window_identity": window_identity,
                    "lease_seconds": lease_seconds,
                    "claimed_at": now,
                    "expires_at": now + lease_seconds,
                }
                claims.append(record)
            self._save(claims)
        return {**self._public(record, include_token=True), "renewed": renewed}

    def assert_access(
        self,
        owner_thread_id: str,
        window_identity: dict[str, Any],
        claim_token: str | None = None,
        *,
        mark_inflight: bool = False,
    ) -> dict[str, Any] | None:
        owner_thread_id = self._validate_owner(owner_thread_id)
        if claim_token is not None and not isinstance(claim_token, str):
            raise ValueError("claim_token must be a string")
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            record = next((claim for claim in claims if self._same_window(claim, window_identity)), None)
            if record is None:
                if changed:
                    self._save(claims)
                if claim_token is not None:
                    raise RuntimeError("claim_token does not name an active claim for this window")
                return None
            if record["owner_thread_id"] != owner_thread_id:
                raise RuntimeError(
                    "window is claimed by another computer-use agent until "
                    f"{record['expires_at']:.3f}"
                )
            if claim_token is None:
                raise ValueError("claim_token is required while this window has an active claim")
            if not secrets.compare_digest(record["token"], claim_token):
                raise ValueError("claim token does not match the active window claim")
            if mark_inflight:
                record["inflight_until"] = now + MAX_INFLIGHT_SECONDS
            else:
                record["expires_at"] = now + record["lease_seconds"]
            self._save(claims)
        return self._public(record, include_token=True)

    def finish_access(
        self,
        owner_thread_id: str,
        window_identity: dict[str, Any],
        claim_token: str,
        *,
        renew: bool,
    ) -> None:
        now = self.clock()
        with self._claims_guard():
            claims, _ = self._load_active(now)
            record = next((claim for claim in claims if self._same_window(claim, window_identity)), None)
            if (
                record is None
                or record.get("owner_thread_id") != owner_thread_id
                or not secrets.compare_digest(str(record.get("token") or ""), claim_token)
            ):
                raise RuntimeError("window claim changed while the claimed operation was running")
            record.pop("inflight_until", None)
            if renew:
                record["expires_at"] = now + record["lease_seconds"]
            self._save(claims)

    def release(
        self,
        owner_thread_id: str,
        claim_token: str,
        *,
        validate_guarded: Callable[[], None] | None = None,
        before_release: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        owner_thread_id = self._validate_owner(owner_thread_id)
        claim_token = self._validate_claim_token(claim_token)
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            record = self._find_by_token(claims, claim_token)
            if record is None:
                if changed:
                    self._save(claims)
                return {"released": False}
            if record["owner_thread_id"] != owner_thread_id:
                raise RuntimeError("only the owning computer-use agent can release this live claim")
            window_identity = record.get("window_identity")
            if not isinstance(window_identity, dict):
                raise RuntimeError("window claim journal contains an invalid window identity")
        with self.window_guard(window_identity):
            if validate_guarded:
                validate_guarded()
            return self.release_while_guarded(
                owner_thread_id,
                claim_token,
                window_identity,
                before_release=before_release,
            )

    def release_while_guarded(
        self,
        owner_thread_id: str,
        claim_token: str,
        window_identity: dict[str, Any],
        *,
        before_release: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Release a claim while the caller holds this window's stable guard."""
        owner_thread_id = self._validate_owner(owner_thread_id)
        claim_token = self._validate_claim_token(claim_token)
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            record = self._find_by_token(claims, claim_token)
            if record is None or not self._same_window(record, window_identity):
                if changed:
                    self._save(claims)
                return {"released": False}
            if record["owner_thread_id"] != owner_thread_id:
                raise RuntimeError("only the owning computer-use agent can release this live claim")
            if before_release:
                before_release(record)
            claims.remove(record)
            self._save(claims)
        return {"released": True, **self._public(record, include_token=True)}

    def list_active(self) -> list[dict[str, Any]]:
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            if changed:
                self._save(claims)
        return [
            self._public(claim)
            for claim in claims
            if claim["session_fingerprint"] == self.session_fingerprint
        ]

    def is_live(
        self,
        owner_thread_id: str,
        window_identity: dict[str, Any],
        claim_token: str | None,
    ) -> bool:
        now = self.clock()
        with self._claims_guard():
            claims, changed = self._load_active(now)
            if changed:
                self._save(claims)
        return any(
            claim["owner_thread_id"] == owner_thread_id
            and self._same_window(claim, window_identity)
            and (claim_token is None or secrets.compare_digest(claim["token"], claim_token))
            for claim in claims
        )

    @contextlib.contextmanager
    def _claims_guard(self):
        self._prepare_directory(self.state_dir)
        descriptor = os.open(self.claims_lock, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self.claims_lock, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _load_active(self, now: float) -> tuple[list[dict[str, Any]], bool]:
        if not self.claims_file.exists():
            return [], False
        try:
            state = json.loads(self.claims_file.read_text())
            claims = state["claims"]
            if state.get("version") != 1 or not isinstance(claims, list):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("window claim journal is malformed; inspect it before continuing") from exc
        active = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and isinstance(claim.get("expires_at"), (int, float))
            and max(claim["expires_at"], float(claim.get("inflight_until") or 0)) > now
        ]
        return active, len(active) != len(claims)

    def _save(self, claims: list[dict[str, Any]]) -> None:
        self._prepare_directory(self.state_dir)
        temporary = self.claims_file.with_name(
            f".{self.claims_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"version": 1, "claims": claims}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.claims_file)
            os.chmod(self.claims_file, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _same_window(self, claim: dict[str, Any], window_identity: dict[str, Any]) -> bool:
        return (
            claim.get("session_fingerprint") == self.session_fingerprint
            and isinstance(claim.get("window_identity"), dict)
            and self._lock_identity(
                claim["window_identity"],
                _WINDOW_LOCK_IDENTITY_KEYS,
            )
            == self._lock_identity(window_identity, _WINDOW_LOCK_IDENTITY_KEYS)
        )

    def _find_by_token(
        self,
        claims: list[dict[str, Any]],
        claim_token: str,
    ) -> dict[str, Any] | None:
        return next(
            (
                claim
                for claim in claims
                if claim.get("session_fingerprint") == self.session_fingerprint
                and secrets.compare_digest(str(claim.get("token") or ""), claim_token)
            ),
            None,
        )

    @staticmethod
    def _public(record: dict[str, Any], *, include_token: bool = False) -> dict[str, Any]:
        result = {
            "owner_thread_id": record["owner_thread_id"],
            "window": record["window"],
            "lease_seconds": record["lease_seconds"],
            "claimed_at": record["claimed_at"],
            "expires_at": record["expires_at"],
        }
        if include_token:
            result["claim_token"] = record["token"]
        return result

    @staticmethod
    def _window_summary(window: dict[str, Any]) -> dict[str, Any]:
        return {
            "xid": str(window.get("xid") or "")[:32],
            "pid": window.get("pid") if type(window.get("pid")) is int else None,
            "wm_class": str(window.get("wm_class") or "")[:80],
            "title": str(window.get("title") or "")[:160],
        }

    @staticmethod
    def _validate_owner(owner_thread_id: str) -> str:
        if not isinstance(owner_thread_id, str) or not owner_thread_id or len(owner_thread_id) > MAX_OWNER_LENGTH:
            raise ValueError(f"MCP _meta.threadId must be a non-empty string of at most {MAX_OWNER_LENGTH} characters")
        return owner_thread_id

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> int:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS
        ):
            raise ValueError(
                f"lease_seconds must be an integer between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
            )
        return lease_seconds

    @staticmethod
    def _validate_claim_token(claim_token: str) -> str:
        if (
            not isinstance(claim_token, str)
            or not claim_token
            or len(claim_token) > MAX_CLAIM_TOKEN_LENGTH
        ):
            raise ValueError(
                "claim_token must be a non-empty string of at most "
                f"{MAX_CLAIM_TOKEN_LENGTH} characters"
            )
        return claim_token

    @staticmethod
    def _prepare_directory(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    @staticmethod
    def _canonical(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _lock_identity(value: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        identity = {key: value.get(key) for key in keys if value.get(key) is not None}
        if not identity:
            raise RuntimeError("cannot lock an object without a stable identity")
        return identity
