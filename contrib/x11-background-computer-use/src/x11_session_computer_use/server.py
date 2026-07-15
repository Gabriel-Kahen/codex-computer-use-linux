import contextlib
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path
from typing import Any

from .capture import build_requirements
from .capture import capture_window
from .capture import ensure_capture_helper


SERVER_INFO = {"name": "x11-same-session-computer-use", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", PROTOCOL_VERSION})
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "x11-same-session-computer-use"
LEASE_FILE = STATE_DIR / "input-lease.json"
LOCK_FILE = STATE_DIR / "input-lease.lock"
MAX_WINDOW_RESULT_BYTES = 32 * 1024
MAX_WINDOWS_PER_PAGE = 20
MAX_WINDOW_TEXT_CHARS = 512


class IdentityMatch(Enum):
    MATCH = "match"
    CHANGED = "changed"
    INDETERMINATE = "indeterminate"


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None, *, read_only: bool = False, idempotent: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": idempotent,
            "openWorldHint": not read_only,
        },
    }


WINDOW = {"window": {"type": "string", "description": "Live XID, exact WM_CLASS, or unique title substring from list_session_windows."}}
TOKEN = {"lease_token": {"type": "string"}}
TOOLS = [
    tool("session_status", "Inspect EWMH X11 session support and the exact safety boundary.", {}, read_only=True, idempotent=True),
    tool("list_session_windows", "List one bounded page of same-UID windows advertised by the current EWMH window manager, including stable-for-lifetime XIDs and AT-SPI correlation hints.", {"cursor": {"type": ["string", "null"]}, "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_WINDOWS_PER_PAGE}}, read_only=True, idempotent=True),
    tool("capture_session_window", "Capture the compositor's exact unobscured pixmap for one mapped X11 window without focusing it, changing desktops, or moving the pointer.", {**WINDOW, "save_path": {"type": ["string", "null"]}}, ["window"], idempotent=True),
    tool("send_window_shortcut", "Best-effort no-focus XSendEvent shortcut delivery. Many modern clients reject synthetic events; use an acknowledged input lease when reliable delivery is required.", {**WINDOW, "key": {"type": "string"}, "modifiers": {"type": "string", "default": ""}}, ["window", "key"]),
    tool("begin_input_lease", "Begin an explicit journaled focus/pointer lease for reliable XTEST input, snapshotting the active desktop, focus, pointer, and target minimized state for restoration.", {**WINDOW, "acknowledge_interference": {"type": "boolean"}}, ["window", "acknowledge_interference"]),
    tool("lease_key", "Send a reliable key or shortcut while the acknowledged target input lease is active.", {**TOKEN, "key": {"type": "string"}, "modifiers": {"type": "string", "default": ""}}, ["lease_token", "key"]),
    tool("lease_pointer_click", "Click a target-window-local coordinate during an acknowledged input lease, then restore the pointer position.", {**TOKEN, "x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "middle", "right"], "default": "left"}, "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1}}, ["lease_token", "x", "y"]),
    tool("lease_pointer_scroll", "Scroll a target-window-local coordinate during an acknowledged input lease, then restore the pointer position.", {**TOKEN, "x": {"type": "integer"}, "y": {"type": "integer"}, "steps": {"type": "integer", "minimum": -20, "maximum": 20}}, ["lease_token", "x", "y", "steps"]),
    tool("lease_pointer_drag", "Drag between target-window-local coordinates during an acknowledged input lease. A journaled pressed-button marker allows crash recovery.", {**TOKEN, "start_x": {"type": "integer"}, "start_y": {"type": "integer"}, "end_x": {"type": "integer"}, "end_y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "middle", "right"], "default": "left"}, "motion_steps": {"type": "integer", "minimum": 2, "maximum": 32, "default": 8}}, ["lease_token", "start_x", "start_y", "end_x", "end_y"]),
    tool("end_input_lease", "Release held synthetic input and restore the pre-lease desktop, focus, pointer, and target minimized state.", TOKEN, ["lease_token"], idempotent=True),
    tool("recover_input_lease", "Recover an unfinished input lease from its journal after interruption or broker failure.", {}, idempotent=True),
]


def run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def _process_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return fields[19]
    except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError):
        return None


def _authenticated_pid(xid: str) -> int | None:
    proc = run([str(ensure_capture_helper()), "--pid", xid])
    return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else None


def _local_display_socket(display: str) -> Path:
    match = re.fullmatch(r"(?:(?:unix)?):([0-9]+)(?:\.[0-9]+)?", display)
    if not match:
        raise RuntimeError("DISPLAY must name a local Unix Xorg server")
    socket = Path("/tmp/.X11-unix") / f"X{match.group(1)}"
    if not socket.exists():
        raise RuntimeError(f"local X11 socket {socket} does not exist")
    return socket


def ensure_session() -> dict[str, Any]:
    display = os.environ.get("DISPLAY")
    if not display:
        raise RuntimeError("DISPLAY is unset; launch Codex from the current graphical X11 login")
    socket = _local_display_socket(display)
    session_id = os.environ.get("XDG_SESSION_ID")
    if not session_id or not shutil.which("loginctl"):
        raise RuntimeError("a logind XDG_SESSION_ID and loginctl are required to verify the local X11 login")
    info = run(["loginctl", "show-session", session_id, "-p", "Active", "-p", "Remote", "-p", "Type", "-p", "Display", "-p", "Leader"])
    if info.returncode:
        raise RuntimeError(info.stderr.strip() or "loginctl could not verify the graphical session")
    values = dict(line.split("=", 1) for line in info.stdout.splitlines() if "=" in line)
    if values.get("Active") != "yes" or values.get("Remote") != "no" or values.get("Type") != "x11":
        raise RuntimeError("DISPLAY is not positively verified as the active local X11 login")
    if _normalized_display(values.get("Display", "")) != _normalized_display(display):
        raise RuntimeError("DISPLAY does not match the X server registered for the logind session")
    probe = run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], timeout=5)
    if probe.returncode:
        raise RuntimeError(probe.stderr.strip() or f"cannot authenticate to X11 display {display}")
    if "not found" in probe.stdout.lower():
        raise RuntimeError("the current X11 display has no EWMH window manager")
    match = re.search(r"window id # (0x[0-9a-fA-F]+)", probe.stdout)
    if not match:
        raise RuntimeError("the EWMH window manager identity is malformed")
    wm_xid = f"0x{int(match.group(1), 16):08x}"
    wm_pid = _authenticated_pid(wm_xid)
    wm_start = _process_start_time(wm_pid) if wm_pid else None
    if not wm_pid or not wm_start or not _pid_belongs_to_session(wm_pid):
        raise RuntimeError("XRes could not authenticate the EWMH window manager to this login")
    socket_stat = socket.stat()
    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise RuntimeError(f"local X11 path {socket} is not a socket")
    final_stat = socket.stat()
    if (socket_stat.st_dev, socket_stat.st_ino) != (final_stat.st_dev, final_stat.st_ino):
        raise RuntimeError("the X11 server socket changed while its identity was verified")
    return {
        "session_id": session_id,
        "leader": values.get("Leader"),
        "display": _normalized_display(display),
        "socket": str(socket),
        "socket_device": final_stat.st_dev,
        "socket_inode": final_stat.st_ino,
        "wm_xid": wm_xid,
        "wm_pid": wm_pid,
        "wm_start_time": wm_start,
    }


def _xprop(xid: str, *properties: str) -> str:
    proc = run(["xprop", "-id", xid, *properties])
    return proc.stdout if proc.returncode == 0 else ""


def _window_state(xid: str) -> dict[str, Any]:
    output = _xprop(xid, "_NET_WM_STATE", "WM_STATE", "_NET_WM_WINDOW_TYPE")
    return {
        "mapped": "Iconic" not in output,
        "minimized": "_NET_WM_STATE_HIDDEN" in output or "Iconic" in output,
        "window_type": next((item for item in re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", output)), None),
    }


def _normalized_display(display: str) -> str:
    match = re.fullmatch(r"(?:(?:unix)?):([0-9]+)(?:\.[0-9]+)?", display)
    return f":{match.group(1)}" if match else display


def _pid_belongs_to_session(pid: int) -> bool:
    process = Path(f"/proc/{pid}")
    try:
        if process.stat().st_uid != os.getuid():
            return False
        environment = {
            key.decode(): value.decode()
            for entry in (process / "environ").read_bytes().split(b"\0")
            if b"=" in entry
            for key, value in [entry.split(b"=", 1)]
        }
    except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeDecodeError):
        return False
    display = environment.get("DISPLAY")
    if not display or _normalized_display(display) != _normalized_display(os.environ.get("DISPLAY", "")):
        return False
    current_session = os.environ.get("XDG_SESSION_ID")
    process_session = environment.get("XDG_SESSION_ID")
    return bool(current_session and process_session == current_session)


def list_windows() -> list[dict[str, Any]]:
    ensure_session()
    proc = run(["wmctrl", "-lpGx"])
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "wmctrl could not enumerate EWMH client windows")
    windows: list[dict[str, Any]] = []
    active = _active_window()
    for line in proc.stdout.splitlines():
        fields = line.split(None, 9)
        if len(fields) < 10:
            continue
        raw_xid, desktop, advertised_pid, x, y, width, height, wm_class, host, title = fields
        wm_class = wm_class[:MAX_WINDOW_TEXT_CHARS]
        host = host[:MAX_WINDOW_TEXT_CHARS]
        title = title[:MAX_WINDOW_TEXT_CHARS]
        try:
            xid = f"0x{int(raw_xid, 16):08x}"
            numeric_pid = _authenticated_pid(xid)
            same_uid = numeric_pid is not None and _pid_belongs_to_session(numeric_pid)
        except ValueError:
            same_uid = False
            numeric_pid = None
            xid = raw_xid.lower()
        state = _window_state(xid)
        if not same_uid:
            continue
        windows.append({
            "xid": xid,
            "capture_id": xid,
            "pid": numeric_pid,
            "advertised_pid": int(advertised_pid) if advertised_pid.isdigit() else None,
            "pid_authenticated_by": "XRes" if numeric_pid else None,
            "same_uid": same_uid,
            "desktop": int(desktop),
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
            "wm_class": wm_class,
            "host": host,
            "title": title,
            "focused": xid == active,
            "mapped": state["mapped"],
            "minimized": state["minimized"],
            "window_type": state["window_type"],
            "xid_lifetime": "stable until this X11 client window is destroyed",
            "at_spi_correlation": {"pid": numeric_pid, "title": title, "wm_class": wm_class},
            "controllable": same_uid,
        })
    return windows


def resolve_window(query: str) -> dict[str, Any]:
    q = query.casefold()
    windows = list_windows()
    exact = [window for window in windows if q in {window["xid"].casefold(), window["wm_class"].casefold()}]
    matches = exact or [window for window in windows if q in window["title"].casefold()]
    if not matches:
        raise RuntimeError(f"no current EWMH window matches {query!r}")
    if len(matches) > 1:
        choices = ", ".join(f'{window["xid"]} {window["wm_class"]} {window["title"]}' for window in matches[:8])
        raise RuntimeError(f"window query is ambiguous; use its XID: {choices}")
    if not matches[0]["same_uid"]:
        raise RuntimeError("refusing to control a window not proven to belong to the current user")
    return matches[0]


def _window_identity(xid: str | None) -> dict[str, Any] | None:
    if not xid:
        return None
    pid = _authenticated_pid(xid)
    start_time = _process_start_time(pid) if pid else None
    if not pid or not start_time or not _pid_belongs_to_session(pid):
        return None
    wm_class = run(["xprop", "-id", xid, "WM_CLASS"])
    if wm_class.returncode:
        return None
    return {"xid": xid, "pid": pid, "process_start_time": start_time, "wm_class": wm_class.stdout[:MAX_WINDOW_TEXT_CHARS].strip()}


def _identity_matches(expected: dict[str, Any] | None) -> IdentityMatch:
    if expected is None:
        return IdentityMatch.CHANGED
    xid = str(expected.get("xid") or "")
    if not xid:
        return IdentityMatch.INDETERMINATE
    try:
        current = _window_identity(xid)
        if current is not None:
            return IdentityMatch.MATCH if current == expected else IdentityMatch.CHANGED
        existence = run(["xprop", "-id", xid, "WM_CLASS"])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return IdentityMatch.INDETERMINATE
    if existence.returncode == 0:
        return IdentityMatch.INDETERMINATE
    error = f"{existence.stdout}\n{existence.stderr}".casefold()
    if "badwindow" in error or "no such window" in error:
        return IdentityMatch.CHANGED
    return IdentityMatch.INDETERMINATE


def _validate_session_binding(state: dict[str, Any]) -> None:
    expected = state.get("session_fingerprint")
    if not isinstance(expected, dict) or ensure_session() != expected:
        raise RuntimeError("refusing lease recovery because the verified X11 login or X server/WM fingerprint changed")


def _validate_lease_binding(state: dict[str, Any]) -> None:
    _validate_session_binding(state)
    target_identity = state.get("target_identity")
    if target_identity:
        identity = _identity_matches(target_identity)
        if identity is IdentityMatch.CHANGED:
            raise RuntimeError("refusing input because the target X11 window identity changed")
        if identity is IdentityMatch.INDETERMINATE:
            raise RuntimeError("refusing input because the target X11 window identity could not be verified")


def _active_window() -> str | None:
    proc = run(["xdotool", "getactivewindow"])
    if proc.returncode or not proc.stdout.strip().isdigit():
        return None
    xid = int(proc.stdout.strip())
    return f"0x{xid:08x}" if xid else None


def _desktop() -> int | None:
    proc = run(["xdotool", "get_desktop"])
    return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else None


def _pointer() -> dict[str, int] | None:
    proc = run(["xdotool", "getmouselocation", "--shell"])
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if proc.returncode or not values.get("X", "").lstrip("-").isdigit() or not values.get("Y", "").lstrip("-").isdigit():
        return None
    return {"x": int(values["X"]), "y": int(values["Y"])}


def _compositor_active() -> bool:
    try:
        name = ctypes.util.find_library("X11")
        if not name:
            return False
        library = ctypes.CDLL(name)
        library.XOpenDisplay.restype = ctypes.c_void_p
        library.XOpenDisplay.argtypes = [ctypes.c_char_p]
        library.XInternAtom.restype = ctypes.c_ulong
        library.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        library.XGetSelectionOwner.restype = ctypes.c_ulong
        library.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = library.XOpenDisplay(None)
        if not display:
            return False
        try:
            screen_text = os.environ.get("DISPLAY", ":0").rsplit(".", 1)
            screen = int(screen_text[1]) if len(screen_text) == 2 and screen_text[1].isdigit() else 0
            atom = library.XInternAtom(ctypes.c_void_p(display), f"_NET_WM_CM_S{screen}".encode(), False)
            return bool(library.XGetSelectionOwner(ctypes.c_void_p(display), atom))
        finally:
            library.XCloseDisplay(ctypes.c_void_p(display))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _lock_state() -> bool | None:
    session_id = os.environ.get("XDG_SESSION_ID")
    if not session_id or not shutil.which("loginctl"):
        return None
    proc = run(["loginctl", "show-session", session_id, "-p", "LockedHint", "--value"])
    if proc.returncode:
        return None
    return {"yes": True, "no": False}.get(proc.stdout.strip())


def _held_physical_input() -> list[str]:
    if not shutil.which("xinput"):
        return ["xinput unavailable, so held physical input cannot be checked"]
    devices = run(["xinput", "list", "--short"])
    if devices.returncode:
        return [devices.stderr.strip() or "xinput could not list physical devices"]
    physical: list[tuple[str, str]] = []
    for line in devices.stdout.splitlines():
        if "XTEST" in line or not re.search(r"slave\s+(pointer|keyboard)", line):
            continue
        match = re.search(r"id=(\d+)", line)
        if match:
            physical.append((match.group(1), line.strip()))
    if not physical:
        return ["xinput reported no non-XTEST slave input devices"]
    held: list[str] = []
    for identity, description in physical:
        state = run(["xinput", "query-state", identity])
        if state.returncode:
            held.append(f"cannot inspect {description}: {state.stderr.strip() or 'query failed'}")
            continue
        for kind, number in re.findall(r"(button|key)\[(\d+)\]=down", state.stdout):
            held.append(f"{description}: {kind} {number}")
    return held


def _ensure_input_safe() -> None:
    locked = _lock_state()
    if locked is None:
        raise RuntimeError("cannot verify that the graphical session is unlocked with loginctl")
    if locked:
        raise RuntimeError("the graphical session is locked")
    held = _held_physical_input()
    if held:
        raise RuntimeError("input lease is unsafe while physical input is held or unobservable: " + ", ".join(held))


@contextlib.contextmanager
def lease_guard():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(LOCK_FILE, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_lease() -> dict[str, Any] | None:
    return json.loads(LEASE_FILE.read_text()) if LEASE_FILE.exists() else None


def save_lease(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    temporary = LEASE_FILE.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(LEASE_FILE)


def require_lease_token(token: str) -> dict[str, Any]:
    state = load_lease()
    if not state:
        raise RuntimeError("no input lease is active")
    if not secrets.compare_digest(str(state.get("token") or ""), token):
        raise ValueError("lease token does not match the active input lease")
    return state


def require_lease(token: str) -> dict[str, Any]:
    state = require_lease_token(token)
    _validate_lease_binding(state)
    return state


def _checked_xdotool(*args: str, timeout: float = 10.0) -> None:
    proc = run(["xdotool", *args], timeout=timeout)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"xdotool {' '.join(args)} failed")


def begin_lease(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true")
    if load_lease():
        raise RuntimeError("an input lease is already active; end or recover it first")
    session_fingerprint = ensure_session()
    _ensure_input_safe()
    target = resolve_window(str(arguments["window"]))
    target_identity = _window_identity(target["xid"])
    if not target_identity or target_identity["pid"] != target["pid"]:
        raise RuntimeError("the target X11 window identity changed before the lease could begin")
    pointer = _pointer()
    if pointer is None:
        raise RuntimeError("cannot snapshot the pointer, so an input lease cannot be restored safely")
    active_xid = _active_window()
    active_identity = _window_identity(active_xid)
    if active_xid and not active_identity:
        raise RuntimeError("cannot authenticate the active window, so focus cannot be restored safely")
    desktop = _desktop()
    if desktop is None:
        raise RuntimeError("cannot snapshot the current desktop, so an input lease cannot be restored safely")
    token = secrets.token_urlsafe(18)
    state = {
        "version": 2,
        "token": token,
        "phase": "prepared",
        "session_fingerprint": session_fingerprint,
        "target": target,
        "target_identity": target_identity,
        "original": {"active_xid": active_xid, "active_identity": active_identity, "desktop": desktop, "pointer": pointer, "target_minimized": target["minimized"]},
        "pressed_button": None,
    }
    save_lease(state)
    try:
        _validate_lease_binding(state)
        _checked_xdotool("windowactivate", "--sync", target["xid"])
        state["phase"] = "active"
        save_lease(state)
    except Exception:
        restore_lease(state)
        raise
    return {
        "lease_token": token,
        "window": target,
        "interference_boundary": "XTEST shares the real keyboard focus and pointer until end_input_lease; every pointer action restores its starting position",
        "journal": str(LEASE_FILE),
    }


BUTTONS = {"left": "1", "middle": "2", "right": "3"}


def _button(arguments: dict[str, Any]) -> str:
    button = str(arguments.get("button") or "left")
    if button not in BUTTONS:
        raise ValueError("button must be left, middle, or right")
    return BUTTONS[button]


def _validate_point(state: dict[str, Any], x: int, y: int) -> None:
    target = state["target"]
    if not (0 <= x < int(target["width"]) and 0 <= y < int(target["height"])):
        raise ValueError(f"coordinate ({x},{y}) is outside window size {target['width']}x{target['height']}")


def _ensure_target_active(state: dict[str, Any]) -> None:
    _ensure_input_safe()
    xid = state["target"]["xid"]
    identity = _identity_matches(state.get("target_identity"))
    if identity is IdentityMatch.CHANGED:
        raise RuntimeError("the leased target window identity changed")
    if identity is IdentityMatch.INDETERMINATE:
        raise RuntimeError("the leased target window identity could not be verified")
    if _active_window() != xid:
        _checked_xdotool("windowactivate", "--sync", xid)


def _shortcut(arguments: dict[str, Any]) -> str:
    key = str(arguments["key"])
    modifiers = str(arguments.get("modifiers") or "")
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+", key):
        raise ValueError("key contains unsupported characters")
    names = [name.lower() for name in modifiers.split()]
    allowed = {"alt", "ctrl", "control", "shift", "super", "meta"}
    if any(name not in allowed for name in names):
        raise ValueError("modifiers may contain only Alt, Ctrl, Control, Shift, Super, or Meta")
    return "+".join([*names, key])


def lease_key(arguments: dict[str, Any]) -> dict[str, Any]:
    state = require_lease(str(arguments["lease_token"]))
    _ensure_target_active(state)
    shortcut = _shortcut(arguments)
    _checked_xdotool("key", "--clearmodifiers", shortcut)
    return {"sent": True, "shortcut": shortcut, "delivery": "XTEST to acknowledged focused lease target"}


def _with_pointer_restore(action) -> None:
    pointer = _pointer()
    if pointer is None:
        raise RuntimeError("cannot snapshot the pointer, so the pointer action was not started")
    try:
        action()
    finally:
        _checked_xdotool("mousemove", "--sync", str(pointer["x"]), str(pointer["y"]))


def lease_pointer(arguments: dict[str, Any], action: str) -> dict[str, Any]:
    state = require_lease(str(arguments["lease_token"]))
    _ensure_target_active(state)
    xid = state["target"]["xid"]
    button = _button(arguments)
    if action in {"click", "scroll"}:
        x, y = int(arguments["x"]), int(arguments["y"])
        _validate_point(state, x, y)
        if action == "click":
            count = int(arguments.get("count", 1))
            if not 1 <= count <= 3:
                raise ValueError("count must be between 1 and 3")
            def perform() -> None:
                _checked_xdotool("mousemove", "--sync", "--window", xid, str(x), str(y))
                _checked_xdotool("click", "--repeat", str(count), "--delay", "40", button)
        else:
            steps = int(arguments["steps"])
            if steps == 0 or abs(steps) > 20:
                raise ValueError("steps must be between -20 and 20, excluding zero")
            wheel = "5" if steps > 0 else "4"
            def perform() -> None:
                _checked_xdotool("mousemove", "--sync", "--window", xid, str(x), str(y))
                _checked_xdotool("click", "--repeat", str(abs(steps)), "--delay", "20", wheel)
        _with_pointer_restore(perform)
    else:
        sx, sy = int(arguments["start_x"]), int(arguments["start_y"])
        ex, ey = int(arguments["end_x"]), int(arguments["end_y"])
        _validate_point(state, sx, sy)
        _validate_point(state, ex, ey)
        steps = int(arguments.get("motion_steps", 8))
        if not 2 <= steps <= 32:
            raise ValueError("motion_steps must be between 2 and 32")
        def perform() -> None:
            _checked_xdotool("mousemove", "--sync", "--window", xid, str(sx), str(sy))
            state["pressed_button"] = button
            save_lease(state)
            _checked_xdotool("mousedown", button)
            try:
                for index in range(1, steps + 1):
                    x = round(sx + (ex - sx) * index / steps)
                    y = round(sy + (ey - sy) * index / steps)
                    _checked_xdotool("mousemove", "--sync", "--window", xid, str(x), str(y))
            finally:
                released = run(["xdotool", "mouseup", button])
                if released.returncode:
                    raise RuntimeError(released.stderr.strip() or "failed to release synthetic drag button; recover_input_lease is required")
                state["pressed_button"] = None
                save_lease(state)
        _with_pointer_restore(perform)
    return {"action": action, "window": state["target"], "pointer_restored": True, "focus_lease_remains_active": True}


def restore_lease(state: dict[str, Any]) -> dict[str, Any]:
    _validate_session_binding(state)
    _ensure_input_safe()
    errors: list[str] = []
    pressed = state.get("pressed_button")
    if pressed:
        proc = run(["xdotool", "mouseup", str(pressed)])
        if proc.returncode:
            errors.append(proc.stderr.strip() or "failed to release journaled mouse button")
    original = state.get("original") or {}
    target = (state.get("target") or {}).get("xid")
    active = original.get("active_xid")
    active_identity = original.get("active_identity")
    active_match = _identity_matches(active_identity) if active_identity else IdentityMatch.CHANGED
    try:
        if active_match is IdentityMatch.MATCH:
            _checked_xdotool("windowactivate", "--sync", str(active))
        else:
            if active_match is IdentityMatch.INDETERMINATE:
                errors.append("original active X11 window identity could not be verified")
            if original.get("desktop") is not None:
                _checked_xdotool("set_desktop", str(original["desktop"]))
    except Exception as exc:
        errors.append(f"focus/desktop restore: {exc}")
    if original.get("target_minimized"):
        target_match = _identity_matches(state.get("target_identity"))
        if target_match is IdentityMatch.MATCH:
            proc = run(["xdotool", "windowminimize", str(target)])
            if proc.returncode:
                errors.append(proc.stderr.strip() or "failed to restore target minimized state")
        elif target_match is IdentityMatch.INDETERMINATE:
            errors.append("target X11 window identity could not be verified; minimized state was not restored")
    pointer = original.get("pointer")
    if pointer:
        proc = run(["xdotool", "mousemove", "--sync", str(pointer["x"]), str(pointer["y"])])
        if proc.returncode:
            errors.append(proc.stderr.strip() or "failed to restore pointer")
    if not errors:
        LEASE_FILE.unlink(missing_ok=True)
    return {"restored": not errors, "errors": errors, "focus": active, "desktop": original.get("desktop"), "pointer": pointer, "released_button": pressed}


def status() -> dict[str, Any]:
    checks = {name: shutil.which(name) is not None for name in ("xprop", "wmctrl", "xdotool", "xinput")}
    session_error = None
    windows: list[dict[str, Any]] = []
    try:
        if checks["xprop"] and checks["wmctrl"]:
            windows = list_windows()
        else:
            session_error = "xprop and wmctrl are required"
    except Exception as exc:
        session_error = str(exc)
    build = build_requirements()
    session_ok = session_error is None
    compositor = _compositor_active() if not session_error else False
    lock_state = _lock_state() if session_ok else None
    return {
        "session": "current local Xorg/EWMH login",
        "display": os.environ.get("DISPLAY"),
        "session_error": session_error,
        "capabilities": {
            "exact_background_window_capture": compositor and all(build.values()) and any(window["mapped"] and not window["minimized"] and window["same_uid"] for window in windows),
            "best_effort_no_focus_shortcuts": session_ok and checks["xdotool"],
            "reliable_journaled_focus_pointer_lease": session_ok and checks["xdotool"] and checks["xinput"] and lock_state is not None,
            "targeted_background_pointer_without_interference": False,
            "background_semantic_actions": False,
        },
        "checks": checks,
        "capture_build_requirements": build,
        "compositing_manager_active": compositor,
        "locked_hint_verified": lock_state is not None,
        "same_uid_window_count": sum(window["same_uid"] for window in windows),
        "unfinished_lease": LEASE_FILE.exists(),
        "semantic_action_companion": "Use the separate Computer Use plugin over AT-SPI, correlated by PID/title/WM_CLASS from list_session_windows.",
        "input_boundary": "X11 has one shared focus and pointer. XSendEvent shortcuts are unconfirmed; reliable XTEST input requires an explicit lease and briefly interferes with the real session.",
    }


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}], "structuredContent": value, "isError": False}


def tool_error(exc: Exception) -> dict[str, Any]:
    value = {"error": str(exc), "type": type(exc).__name__}
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value, "isError": True}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "session_status":
        return text_result(status())
    if name == "list_session_windows":
        limit = arguments.get("limit", MAX_WINDOWS_PER_PAGE)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_WINDOWS_PER_PAGE:
            raise ValueError(f"limit must be an integer between 1 and {MAX_WINDOWS_PER_PAGE}")
        cursor = arguments.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and cursor.isascii() and cursor.isdigit():
            offset = int(cursor)
        else:
            raise ValueError("cursor must be the next_cursor string from a previous result")
        windows = list_windows()
        page: list[dict[str, Any]] = []
        end = offset
        while end < len(windows) and len(page) < limit:
            candidate = [*page, windows[end]]
            candidate_end = end + 1
            candidate_result = {
                "windows": candidate,
                "next_cursor": str(candidate_end) if candidate_end < len(windows) else None,
            }
            encoded = json.dumps(candidate_result, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_WINDOW_RESULT_BYTES:
                break
            page = candidate
            end = candidate_end
        if not page and offset < len(windows):
            raise RuntimeError("a window entry exceeds the bounded listing result size")
        return text_result({"windows": page, "next_cursor": str(end) if end < len(windows) else None})
    if name == "capture_session_window":
        return capture_window(resolve_window(str(arguments["window"])), arguments.get("save_path"))
    if name == "send_window_shortcut":
        with lease_guard():
            if load_lease():
                raise RuntimeError("end or recover the active input lease before sending a direct shortcut")
            window = resolve_window(str(arguments["window"]))
            shortcut = _shortcut(arguments)
            ensure_session()
            _ensure_input_safe()
            proc = run(["xdotool", "key", "--window", window["xid"], shortcut])
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip() or "best-effort XSendEvent shortcut failed")
            return text_result({"sent": True, "delivery_confirmed": False, "mechanism": "XSendEvent", "window": window, "shortcut": shortcut, "focus_changed": False})
    if name == "begin_input_lease":
        with lease_guard():
            return text_result(begin_lease(arguments))
    if name == "lease_key":
        with lease_guard():
            return text_result(lease_key(arguments))
    if name.startswith("lease_pointer_"):
        with lease_guard():
            return text_result(lease_pointer(arguments, name.removeprefix("lease_pointer_")))
    if name == "end_input_lease":
        with lease_guard():
            return text_result(restore_lease(require_lease_token(str(arguments["lease_token"]))))
    if name == "recover_input_lease":
        with lease_guard():
            state = load_lease()
            return text_result({"restored": True, "message": "no unfinished input lease"} if not state else restore_lease(state))
    raise ValueError(f"unknown tool: {name}")


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        method = message.get("method")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            negotiated = requested if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            result = {"protocolVersion": negotiated, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO, "instructions": "Operate only same-UID windows in the current local EWMH Xorg login. Prefer AT-SPI through the separate Computer Use plugin; use exact XComposite capture here, and acknowledge a journaled input lease for reliable coordinate or keyboard input."}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            try:
                result = call_tool(str(params.get("name") or ""), arguments)
            except Exception as exc:
                result = tool_error(exc)
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def main() -> int:
    output_lock = threading.Lock()
    workers: list[threading.Thread] = []
    def process(message: dict[str, Any]) -> None:
        response = dispatch(message)
        if response is not None:
            with output_lock:
                print(json.dumps(response, separators=(",", ":")), flush=True)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except Exception as exc:
            with output_lock:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}), flush=True)
            continue
        worker = threading.Thread(target=process, args=(message,))
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join()
    return 0
