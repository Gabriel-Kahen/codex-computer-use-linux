import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator


MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
DEFAULT_LEASE_SECONDS = 60
MAX_ACTIVE_CLAIMS = 128
MAX_OWNER_CHARS = 128
MAX_INFLIGHT_SECONDS = 300


def current_session_identity() -> str:
    """Return an opaque identity for one Unix user's GNOME login session."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    fields = {
        "uid": os.getuid(),
        "xdg_session_id": os.environ.get("XDG_SESSION_ID", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "display": os.environ.get("DISPLAY", ""),
        "dbus_session_bus_address": os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
        ),
        "xdg_runtime_dir": runtime_dir,
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return f"gnome-{hashlib.sha256(encoded).hexdigest()[:32]}"


def process_start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except (IndexError, OSError):
        return None


def current_broker_identity() -> dict[str, Any]:
    pid = os.getpid()
    return {"pid": pid, "start_time": process_start_time(pid)}


def broker_is_alive(broker: Any) -> bool:
    if not isinstance(broker, dict) or type(broker.get("pid")) is not int:
        return True
    pid = broker["pid"]
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    expected_start = broker.get("start_time")
    return expected_start is None or process_start_time(pid) == expected_start


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


@contextmanager
def file_guard(path: Path) -> Iterator[None]:
    ensure_private_directory(path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_owner(owner: str | None) -> str:
    if not isinstance(owner, str) or not owner or len(owner) > MAX_OWNER_CHARS or "\0" in owner:
        raise ValueError(
            f"window claims require params._meta.threadId containing 1 to {MAX_OWNER_CHARS} characters"
        )
    return owner


def _validate_token(token: Any) -> str:
    if not isinstance(token, str) or not 64 <= len(token) <= 256:
        raise ValueError("claim_token has an invalid length")
    return token


def _validate_lease_seconds(value: Any) -> int:
    if type(value) is not int or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be an integer between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )
    return value


class ClaimRegistry:
    def __init__(
        self,
        state_dir: Path,
        session_identity: str,
        *,
        clock: Callable[[], float] = time.time,
        process_alive: Callable[[Any], bool] = broker_is_alive,
        broker: dict[str, Any] | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.session_identity = session_identity
        self.clock = clock
        self.process_alive = process_alive
        self.broker = broker or current_broker_identity()
        self.claims_file = state_dir / "window-claims.json"
        self.lock_file = state_dir / "window-claims.lock"
        self.window_lock_dir = state_dir / "window-locks"

    def _empty_state(self) -> dict[str, Any]:
        return {"version": 1, "session_identity": self.session_identity, "claims": {}}

    def _load(self) -> dict[str, Any]:
        if not self.claims_file.exists():
            return self._empty_state()
        try:
            state = json.loads(self.claims_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read the private window-claim journal: {exc}") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("session_identity") != self.session_identity
            or not isinstance(state.get("claims"), dict)
        ):
            raise RuntimeError("window-claim journal belongs to another session or has an unsupported format")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.claims_file, state)

    def _window_lock(self, window_id: str) -> Path:
        digest = hashlib.sha256(window_id.encode()).hexdigest()
        return self.window_lock_dir / f"{digest}.lock"

    def _live(self, claim: dict[str, Any], shell_instance: str, now: float) -> bool:
        deadlines = [
            value
            for value in (claim.get("expires_at"), claim.get("inflight_until"))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        effective_expiry = max(deadlines, default=None)
        return (
            effective_expiry is not None
            and effective_expiry > now
            and claim.get("shell_instance") == shell_instance
            and claim.get("session_identity") == self.session_identity
            and self.process_alive(claim.get("broker"))
        )

    def _public(self, claim: dict[str, Any]) -> dict[str, Any]:
        window = claim["window"]
        return {
            "window": {
                "id": str(window.get("id") or "")[:128],
                "title": str(window.get("title") or "")[:128],
                "app_id": str(window.get("app_id") or "")[:96],
            },
            "owner_thread_id": claim["owner_thread_id"],
            "claimed_at": claim["claimed_at"],
            "expires_at": claim["expires_at"],
            "lease_seconds": claim["lease_seconds"],
        }

    def claim(
        self,
        window: dict[str, Any],
        owner: str | None,
        lease_seconds: Any,
        shell_instance: str,
        *,
        before_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        owner = _validate_owner(owner)
        lease_seconds = _validate_lease_seconds(lease_seconds)
        window_id = str(window.get("id") or "")
        if not window_id:
            raise ValueError("cannot claim a window without a stable id")
        with file_guard(self._window_lock(window_id)):
            started_at = self.clock()
            with file_guard(self.lock_file):
                state = self._load()
                recorded = state["claims"].get(window_id)
                live_claims = {
                    candidate_id: candidate
                    for candidate_id, candidate in state["claims"].items()
                    if self._live(candidate, shell_instance, started_at)
                }
                previous = live_claims.get(window_id)
                if previous and previous.get("owner_thread_id") != owner:
                    raise RuntimeError(
                        f"window {window_id} is already claimed by another computer-use agent"
                    )
                if previous:
                    token = previous["claim_token"]
                    claimed_at = previous["claimed_at"]
                else:
                    if len(live_claims) >= MAX_ACTIVE_CLAIMS:
                        raise RuntimeError(
                            f"this GNOME session already has the maximum {MAX_ACTIVE_CLAIMS} live window claims"
                        )
                    token = secrets.token_hex(32)
                    claimed_at = started_at
                claim = {
                    "session_identity": self.session_identity,
                    "shell_instance": shell_instance,
                    "window": {
                        "id": str(window.get("id") or "")[:512],
                        "title": str(window.get("title") or "")[:512],
                        "app_id": str(window.get("app_id") or "")[:256],
                    },
                    "owner_thread_id": owner,
                    "claim_token": token,
                    "broker": self.broker,
                    "claimed_at": claimed_at,
                    "expires_at": previous["expires_at"] if previous else started_at,
                    "lease_seconds": lease_seconds,
                    "inflight_until": started_at + MAX_INFLIGHT_SECONDS,
                }
                state["claims"] = {
                    candidate_id: candidate
                    for candidate_id, candidate in state["claims"].items()
                    if candidate_id == window_id
                    or self._live(candidate, shell_instance, started_at)
                }
                if window_id not in state["claims"] and len(state["claims"]) >= MAX_ACTIVE_CLAIMS:
                    raise RuntimeError(
                        f"this GNOME session already has the maximum {MAX_ACTIVE_CLAIMS} live window claims"
                    )
                state["claims"][window_id] = claim
                self._save(state)
            try:
                if before_save:
                    before_save(claim)
            except Exception:
                with file_guard(self.lock_file):
                    state = self._load()
                    current = state["claims"].get(window_id)
                    if current and secrets.compare_digest(current["claim_token"], token):
                        if recorded is None:
                            del state["claims"][window_id]
                        else:
                            state["claims"][window_id] = recorded
                        self._save(state)
                raise
            completed_at = self.clock()
            with file_guard(self.lock_file):
                state = self._load()
                current = state["claims"].get(window_id)
                if current is None or not secrets.compare_digest(current["claim_token"], token):
                    raise RuntimeError("window claim changed while it was being established")
                claim["claimed_at"] = previous["claimed_at"] if previous else completed_at
                claim["expires_at"] = completed_at + lease_seconds
                claim.pop("inflight_until", None)
                state["claims"][window_id] = claim
                self._save(state)
        return {
            **self._public(claim),
            "claim_token": token,
            "renewed": previous is not None,
        }

    @contextmanager
    def authorize(
        self,
        window_id: str,
        owner: str | None,
        claim_token: Any,
        shell_instance: str,
        *,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterator[dict[str, Any] | None]:
        token = _validate_token(claim_token) if claim_token is not None else None
        with file_guard(self._window_lock(window_id)):
            with file_guard(self.lock_file):
                state = self._load()
                claim = state["claims"].get(window_id)
                now = self.clock()
                if claim and not self._live(claim, shell_instance, now):
                    del state["claims"][window_id]
                    self._save(state)
                    claim = None
                if claim:
                    if claim.get("owner_thread_id") != owner:
                        raise RuntimeError(
                            f"window {window_id} is claimed by another computer-use agent"
                        )
                    if token is None:
                        raise ValueError("claim_token is required while this window has an active claim")
                    if not secrets.compare_digest(claim["claim_token"], token):
                        raise ValueError("claim_token does not match the active window claim")
                elif token is not None:
                    raise RuntimeError("claim_token is expired or does not identify an active window claim")
                if claim:
                    claim["inflight_until"] = now + MAX_INFLIGHT_SECONDS
                    state["claims"][window_id] = claim
                    self._save(state)
            succeeded = False
            try:
                yield claim
                if claim and on_complete:
                    on_complete(claim)
                succeeded = True
            finally:
                if claim:
                    with file_guard(self.lock_file):
                        state = self._load()
                        current = state["claims"].get(window_id)
                        if current and secrets.compare_digest(current["claim_token"], claim["claim_token"]):
                            current.pop("inflight_until", None)
                            if succeeded:
                                current["expires_at"] = self.clock() + current["lease_seconds"]
                                current["broker"] = self.broker
                            state["claims"][window_id] = current
                            self._save(state)

    @contextmanager
    def inspect(self, window_id: str, shell_instance: str) -> Iterator[dict[str, Any] | None]:
        """Hold a window lane while recovery inspects its live claim."""
        with file_guard(self._window_lock(window_id)):
            with file_guard(self.lock_file):
                state = self._load()
                claim = state["claims"].get(window_id)
                if claim and not self._live(claim, shell_instance, self.clock()):
                    claim = None
            yield claim

    def release(
        self,
        claim_token: Any,
        owner: str | None,
        shell_instance: str,
        *,
        before_release: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        token = _validate_token(claim_token)
        owner = _validate_owner(owner)
        with file_guard(self.lock_file):
            state = self._load()
            matched = next(
                (claim for claim in state["claims"].values() if secrets.compare_digest(claim["claim_token"], token)),
                None,
            )
            if matched is None:
                return {"released": False, "message": "claim is already absent or expired"}
            window_id = str(matched["window"]["id"])
        with file_guard(self._window_lock(window_id)):
            with file_guard(self.lock_file):
                state = self._load()
                claim = state["claims"].get(window_id)
                if claim is None or not secrets.compare_digest(claim["claim_token"], token):
                    return {"released": False, "message": "claim is already absent or expired"}
                if self._live(claim, shell_instance, self.clock()) and claim.get("owner_thread_id") != owner:
                    raise RuntimeError("a live window claim can only be released by its owning thread")
                if claim.get("owner_thread_id") != owner:
                    raise RuntimeError("claim_token belongs to another computer-use agent")
            if before_release:
                before_release(claim)
            with file_guard(self.lock_file):
                state = self._load()
                current = state["claims"].get(window_id)
                if current is None or not secrets.compare_digest(current["claim_token"], token):
                    return {"released": False, "message": "claim is already absent or expired"}
                del state["claims"][window_id]
                self._save(state)
        return {"released": True, "window": self._public(claim)["window"]}

    def list(self, shell_instance: str) -> list[dict[str, Any]]:
        now = self.clock()
        with file_guard(self.lock_file):
            state = self._load()
            return [
                self._public(claim)
                for claim in state["claims"].values()
                if self._live(claim, shell_instance, now)
            ][:MAX_ACTIVE_CLAIMS]
