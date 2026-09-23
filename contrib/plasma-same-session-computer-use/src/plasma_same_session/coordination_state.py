import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import kwin


MAX_THREAD_ID_CHARS = 200
MAX_THREAD_ID_BYTES = 512
MAX_WINDOW_ID_CHARS = 80
MAX_WINDOW_ID_BYTES = 320
MAX_WINDOW_TITLE_CHARS = 160
MAX_WINDOW_TITLE_BYTES = 160
MAX_WINDOW_CLASS_CHARS = 96
MAX_WINDOW_CLASS_BYTES = 96
MAX_WINDOW_COORDINATE = 1_000_000
MAX_WINDOW_DIMENSION = 1_000_000
MAX_WINDOW_QUERY_CHARS = 256
MAX_WINDOW_QUERY_BYTES = 1024
MAX_CLAIM_TOKEN_CHARS = 160
CLAIM_TOKEN_PATTERN = r"[0-9a-f]{64}\.[A-Za-z0-9_-]{32,64}"


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def truncate_json_text(value: Any, max_chars: int, max_bytes: int) -> str:
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


def optional_desktop(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not -(2**31) <= value <= 2**31 - 1:
        raise RuntimeError(f"{name} is invalid")
    return value


def optional_pointer(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("pointer position is invalid")
    pointer = {name: value.get(name) for name in ("x", "y")}
    if any(
        type(coordinate) is not int or abs(coordinate) > MAX_WINDOW_COORDINATE
        for coordinate in pointer.values()
    ):
        raise RuntimeError("pointer position is invalid")
    return pointer


def window_for_model(window: dict[str, Any]) -> dict[str, Any]:
    window_id = require_window_id(window.get("id"))
    capture_id = require_window_id(window.get("capture_id"))
    geometry = window.get("geometry")
    if not isinstance(geometry, dict):
        raise RuntimeError("window geometry is invalid")
    bounded_geometry = {}
    for name in ("x", "y", "width", "height"):
        value = geometry.get(name)
        limit = MAX_WINDOW_DIMENSION if name in {"width", "height"} else MAX_WINDOW_COORDINATE
        minimum = 0 if name in {"width", "height"} else -limit
        if type(value) is not int or not minimum <= value <= limit:
            raise RuntimeError(f"window geometry {name} is invalid")
        bounded_geometry[name] = value
    pid = window.get("pid")
    desktop = window.get("desktop")
    if pid is not None and (type(pid) is not int or not 0 <= pid <= 2**31 - 1):
        raise RuntimeError("window pid is invalid")
    if desktop is not None and (type(desktop) is not int or not -(2**31) <= desktop <= 2**31 - 1):
        raise RuntimeError("window desktop is invalid")
    flags = {}
    for name in ("active", "minimized", "fullscreen", "excluded_from_capture"):
        value = window.get(name)
        if value is not None and type(value) is not bool:
            raise RuntimeError(f"window {name} state is invalid")
        flags[name] = value
    return {
        "id": window_id,
        "capture_id": capture_id,
        "title": truncate_json_text(window.get("title"), MAX_WINDOW_TITLE_CHARS, MAX_WINDOW_TITLE_BYTES),
        "class": truncate_json_text(window.get("class"), MAX_WINDOW_CLASS_CHARS, MAX_WINDOW_CLASS_BYTES),
        "pid": pid,
        "desktop": desktop,
        **flags,
        "geometry": bounded_geometry,
    }


def window_summary_for_model(window: dict[str, Any]) -> dict[str, Any]:
    bounded = window_for_model(window)
    return {name: bounded[name] for name in ("id", "capture_id", "title", "class")}


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
