import base64
import fcntl
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    from gi.repository import GLib
except (ImportError, ValueError):
    Gio = None
    GLib = None


SERVER_INFO = {"name": "gnome-same-session-computer-use", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_STDOUT_LINE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_PNG_BYTES = 5 * 1024 * 1024
BUS_NAME = "org.gnome.Shell.Extensions.BackgroundComputerUse"
OBJECT_PATH = "/org/gnome/Shell/Extensions/BackgroundComputerUse"
INTERFACE = BUS_NAME
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / SERVER_INFO["name"]
LEASE_FILE = STATE_DIR / "focus-lease.json"
LOCK_FILE = STATE_DIR / "focus-lease.lock"
INPUT_FILE = STATE_DIR / "input.lock"
INPUT_LOCK = threading.Lock()
DBUS_LOCK = threading.Lock()
_DBUS_CONNECTION = None


def tool(name: str, description: str, properties: dict[str, Any], required: list[str], *, read_only: bool = False, idempotent: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": idempotent,
            "openWorldHint": not read_only,
        },
    }


WINDOW = {"type": "string", "description": "Stable GNOME window id, exact app id/class, or title substring."}
TOKEN = {"type": "string", "description": "Token returned by begin_focus_lease."}
POINT = {"type": "number", "minimum": 0}
TOOLS = [
    tool("session_status", "Report GNOME/Mutter integration health and exact capability boundaries.", {}, [], read_only=True, idempotent=True),
    tool("list_session_windows", "List existing windows in the user's real GNOME Shell session.", {}, [], read_only=True, idempotent=True),
    tool("capture_session_window", "Capture an exact focused window. An unfocused window must first be placed under an acknowledged focus lease.", {"window": WINDOW, "save_path": {"type": ["string", "null"]}}, ["window"], idempotent=True),
    tool("begin_focus_lease", "Journal desktop state, switch to and focus an existing window, and authorize brief global-seat contention until restored.", {"window": WINDOW, "acknowledge_interference": {"type": "boolean"}}, ["window", "acknowledge_interference"]),
    tool("lease_pointer_click", "Click a leased window using Mutter's global virtual seat, restoring the pointer immediately afterward.", {"lease_token": TOKEN, "x": POINT, "y": POINT, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}, "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1}}, ["lease_token", "x", "y"]),
    tool("lease_pointer_scroll", "Scroll in a leased window using Mutter's global virtual seat, restoring the pointer immediately afterward.", {"lease_token": TOKEN, "x": POINT, "y": POINT, "steps": {"type": "integer", "minimum": -20, "maximum": 20}}, ["lease_token", "x", "y", "steps"]),
    tool("lease_pointer_drag", "Drag in a leased window using Mutter's global virtual seat, restoring the pointer after release.", {"lease_token": TOKEN, "start_x": POINT, "start_y": POINT, "end_x": POINT, "end_y": POINT, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}, "motion_steps": {"type": "integer", "minimum": 2, "maximum": 32, "default": 8}}, ["lease_token", "start_x", "start_y", "end_x", "end_y"]),
    tool("send_lease_shortcut", "Send one key or shortcut to the currently focused leased window through Mutter's global virtual keyboard.", {"lease_token": TOKEN, "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string", "enum": ["CTRL", "SHIFT", "ALT", "SUPER"]}, "default": []}}, ["lease_token", "key"]),
    tool("end_focus_lease", "Restore the pre-lease workspace, focused window, and pointer from the journal.", {"lease_token": TOKEN}, ["lease_token"]),
    tool("recover_focus_lease", "Restore any unfinished focus lease after a broker interruption or crash.", {}, [], idempotent=True),
]


def run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def ensure_session_environment() -> None:
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus")


def dbus_call(method: str, *arguments: str) -> Any:
    global _DBUS_CONNECTION
    ensure_session_environment()
    if Gio is None or GLib is None:
        raise RuntimeError("PyGObject (python3-gobject) is required for the persistent GNOME session-bus connection")
    try:
        with DBUS_LOCK:
            if _DBUS_CONNECTION is None or _DBUS_CONNECTION.is_closed():
                _DBUS_CONNECTION = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            parameters = GLib.Variant(f"({'s' * len(arguments)})", arguments)
            response = _DBUS_CONNECTION.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                method,
                parameters,
                GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE,
                10_000,
                None,
            )
        return json.loads(response.unpack()[0])
    except Exception as exc:
        raise RuntimeError(f"GNOME integration method {method} failed: {exc}") from exc


def windows() -> list[dict[str, Any]]:
    value = dbus_call("ListWindows")
    if not isinstance(value, list):
        raise RuntimeError("GNOME integration returned an invalid window list")
    return value


def resolve_window(query: str) -> dict[str, Any]:
    if not query or len(query) > 512:
        raise ValueError("window must contain between 1 and 512 characters")
    candidates = windows()
    lowered = query.casefold()
    exact = [window for window in candidates if lowered in {
        str(window.get("id") or "").casefold(),
        str(window.get("app_id") or "").casefold(),
        str(window.get("wm_class") or "").casefold(),
    }]
    matches = exact or [window for window in candidates if lowered in str(window.get("title") or "").casefold()]
    if not matches:
        raise RuntimeError(f"no real GNOME window matches {query!r}")
    if len(matches) > 1:
        choices = ", ".join(f"{window.get('app_id')} {window.get('title')} ({window.get('id')})" for window in matches[:8])
        raise RuntimeError(f"window query is ambiguous; use a stable id: {choices}")
    return matches[0]


@contextmanager
def file_guard(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_lease() -> dict[str, Any] | None:
    if not LEASE_FILE.exists():
        return None
    return json.loads(LEASE_FILE.read_text())


def save_lease(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = LEASE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.chmod(0o600)
    temporary.replace(LEASE_FILE)


def begin_lease(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true because GNOME uses one global input seat")
    if load_lease():
        raise RuntimeError("a focus lease is already active; end or recover it first")
    selected = resolve_window(str(arguments["window"]))
    prepared = dbus_call("BeginLease", str(selected["id"]))
    capability = prepared.get("capability")
    if not isinstance(capability, str) or len(capability) < 64:
        raise RuntimeError("GNOME integration returned an invalid lease capability")
    state = {
        "version": 2,
        "token": capability,
        "phase": "prepared",
        "target": prepared.get("target") or selected,
        "original": prepared.get("original"),
        "shell_instance": prepared.get("shell_instance"),
    }
    try:
        save_lease(state)
        focused = dbus_call("ActivateLease", capability)
        if str((focused.get("state") or {}).get("focused_window")) != str(selected["id"]):
            raise RuntimeError("GNOME did not grant focus to the requested lease window")
        state["phase"] = "active"
        save_lease(state)
    except Exception:
        restore_lease(state)
        raise
    return {
        "lease_token": state["token"],
        "window": selected,
        "interference_boundary": "workspace and keyboard focus remain leased; pointer is briefly moved and restored per pointer action",
        "capture": "capture_session_window is exact while this lease remains active",
    }


def require_lease(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not 64 <= len(token) <= 256:
        raise ValueError("lease_token has an invalid length")
    state = load_lease()
    if not state:
        raise RuntimeError("no focus lease is active")
    if not secrets.compare_digest(str(state.get("token") or ""), token):
        raise ValueError("lease token does not match the active focus lease")
    return state


def restore_lease(state: dict[str, Any], *, recovery: bool = False) -> dict[str, Any]:
    try:
        result = dbus_call("RecoverLease" if recovery else "RestoreLease", str(state["token"]))
    except Exception as exc:
        result = {"restored": False, "errors": [str(exc)]}
        if recovery and state.get("phase") == "prepared" and state.get("shell_instance"):
            try:
                shell = dbus_call("Status")
            except Exception:
                shell = None
            if (
                isinstance(shell, dict)
                and shell.get("shell_instance") == state["shell_instance"]
                and shell.get("lease_phase") is None
            ):
                result = {"restored": True, "errors": [], "state": shell, "expired_pending_lease": True}
    if not isinstance(result, dict):
        result = {"restored": False, "errors": ["GNOME integration returned an invalid restoration result"]}
    restored = result.get("restored") is True and not result.get("errors")
    if restored:
        LEASE_FILE.unlink(missing_ok=True)
    return {
        "restored": restored,
        "errors": result.get("errors") or [],
        "post_restore_state": result.get("state"),
        "expired_pending_lease": result.get("expired_pending_lease") is True,
        "target": (state.get("target") or {}).get("id"),
        "journal_retained": not restored,
    }


def png_pixel_size(raw: bytes) -> dict[str, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return {"width": int.from_bytes(raw[16:20], "big"), "height": int.from_bytes(raw[20:24], "big")}


def coordinate_space(frame: dict[str, Any], raw: bytes) -> dict[str, Any]:
    pixels = png_pixel_size(raw)
    width = frame.get("width")
    height = frame.get("height")
    transform = None
    if pixels and isinstance(width, (int, float)) and isinstance(height, (int, float)) and pixels["width"] and pixels["height"]:
        transform = {"x": float(width) / pixels["width"], "y": float(height) / pixels["height"]}
    return {
        "window_local": {"width": width, "height": height},
        "screenshot_pixels": pixels,
        "pixel_to_window_scale": transform,
        "note": "Pointer tools use logical window-local coordinates; multiply screenshot x/y by pixel_to_window_scale.",
    }


def capture_window(arguments: dict[str, Any]) -> dict[str, Any]:
    selected = resolve_window(str(arguments["window"]))
    active_lease = load_lease()
    permitted = selected.get("focused") or (
        active_lease and active_lease.get("phase") == "active" and
        str((active_lease.get("target") or {}).get("id")) == str(selected.get("id"))
    )
    if not permitted:
        raise RuntimeError("stock Mutter can only capture the focused window exactly; begin_focus_lease first")
    destination_value = arguments.get("save_path")
    destination = Path(str(destination_value)).expanduser() if destination_value else None
    if destination and not destination.is_absolute():
        raise ValueError("save_path must be absolute")
    directory = destination.parent if destination else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}." if destination else "gnome-window-", suffix=".png", dir=directory)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink(missing_ok=True)
    try:
        proc = run([
            "gdbus", "call", "--session", "--dest", "org.gnome.Shell.Screenshot",
            "--object-path", "/org/gnome/Shell/Screenshot", "--method",
            "org.gnome.Shell.Screenshot.ScreenshotWindow", "true", "false", "false", str(temporary),
        ], timeout=20)
        if proc.returncode or not temporary.is_file():
            if not shutil.which("gnome-screenshot"):
                raise RuntimeError(proc.stderr.strip() or "GNOME focused-window screenshot service failed")
            fallback = run(["gnome-screenshot", "-w", "-f", str(temporary)], timeout=20)
            if fallback.returncode or not temporary.is_file():
                raise RuntimeError(fallback.stderr.strip() or "focused-window capture failed")
        current = resolve_window(str(selected["id"]))
        if not current.get("focused"):
            raise RuntimeError("the leased window lost focus during capture; screenshot discarded")
        temporary.chmod(0o600)
        with temporary.open("rb") as image:
            raw = image.read(MAX_CAPTURE_PNG_BYTES + 1)
        if len(raw) > MAX_CAPTURE_PNG_BYTES:
            raise RuntimeError(
                f"captured PNG exceeds the {MAX_CAPTURE_PNG_BYTES}-byte MCP transport limit"
            )
        if destination:
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    frame = selected.get("frame") or {}
    metadata = {
        "window": selected,
        "saved_to": str(destination) if destination else None,
        "coordinate_space": coordinate_space(frame, raw),
        "capture_requires_focus": True,
        "focus_changed_by_capture": False,
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps(metadata, indent=2)},
            {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"},
        ],
        "isError": False,
    }


def finite_coordinate(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


def bounded_integer(value: Any, name: str, minimum: int, maximum: int, *, exclude_zero: bool = False) -> int:
    if type(value) is not int or not minimum <= value <= maximum or (exclude_zero and value == 0):
        qualifier = ", excluding zero" if exclude_zero else ""
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}{qualifier}")
    return value


def pointer_action(arguments: dict[str, Any], action: str) -> dict[str, Any]:
    if action not in {"click", "scroll", "drag"}:
        raise ValueError(f"unknown pointer action {action}")
    button = arguments.get("button", "left")
    if button not in {"left", "right", "middle"}:
        raise ValueError("button must be left, right, or middle")
    request: dict[str, Any] = {"action": action, "button": button}
    if action == "drag":
        request.update({
            "start": {"x": finite_coordinate(arguments["start_x"], "start_x"), "y": finite_coordinate(arguments["start_y"], "start_y")},
            "end": {"x": finite_coordinate(arguments["end_x"], "end_x"), "y": finite_coordinate(arguments["end_y"], "end_y")},
            "motion_steps": bounded_integer(arguments.get("motion_steps", 8), "motion_steps", 2, 32),
        })
    else:
        request["point"] = {"x": finite_coordinate(arguments["x"], "x"), "y": finite_coordinate(arguments["y"], "y")}
        if action == "click":
            request["count"] = bounded_integer(arguments.get("count", 1), "count", 1, 3)
        else:
            request["steps"] = bounded_integer(arguments["steps"], "steps", -20, 20, exclude_zero=True)
    with file_guard(LOCK_FILE):
        state = require_lease(arguments.get("lease_token"))
        with INPUT_LOCK, file_guard(INPUT_FILE):
            result = dbus_call("InjectPointer", state["token"], json.dumps(request, separators=(",", ":")))
    return {"lease_token": state["token"], "window": state["target"], "transaction": result, "global_seat_used": True}


def send_shortcut(arguments: dict[str, Any]) -> dict[str, Any]:
    modifiers = arguments.get("modifiers", [])
    allowed = {"CTRL", "SHIFT", "ALT", "SUPER"}
    if not isinstance(modifiers, list) or len(modifiers) > 4 or any(type(value) is not str or value not in allowed for value in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError("modifiers must be a unique array containing at most CTRL, SHIFT, ALT, and SUPER")
    key = arguments.get("key")
    if not isinstance(key, str) or not key or len(key) > 64:
        raise ValueError("key must contain between 1 and 64 characters")
    request = {"key": key, "modifiers": modifiers}
    with file_guard(LOCK_FILE):
        state = require_lease(arguments.get("lease_token"))
        with INPUT_LOCK, file_guard(INPUT_FILE):
            result = dbus_call("InjectKeys", state["token"], json.dumps(request, separators=(",", ":")))
    return {"window": state["target"], "transaction": result, "global_seat_used": True}


def status() -> dict[str, Any]:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    checks = {
        "gdbus": shutil.which("gdbus") is not None,
        "pygobject": Gio is not None and GLib is not None,
        "gnome_session": "GNOME" in desktop.upper(),
    }
    integration: dict[str, Any] | None = None
    error: str | None = None
    if checks["pygobject"]:
        try:
            integration = dbus_call("Status")
        except Exception as exc:
            error = str(exc)
    screenshot_service = False
    if checks["gdbus"]:
        ensure_session_environment()
        screenshot = run([
            "gdbus", "introspect", "--session", "--dest", "org.gnome.Shell.Screenshot",
            "--object-path", "/org/gnome/Shell/Screenshot",
        ])
        screenshot_service = screenshot.returncode == 0 and "ScreenshotWindow" in screenshot.stdout
    checks["focused_window_screenshot"] = screenshot_service or shutil.which("gnome-screenshot") is not None
    ready = integration is not None
    return {
        "desktop": desktop,
        "integration": integration,
        "integration_error": error,
        "capabilities": {
            "window_enumeration": ready,
            "stable_window_ids": ready,
            "exact_background_window_capture": False,
            "exact_focused_window_capture": ready and checks["focused_window_screenshot"],
            "background_semantic_actions": False,
            "targeted_background_pointer": False,
            "targeted_background_keyboard": False,
            "recoverable_focus_lease": ready,
            "lease_pointer_restoration": ready,
        },
        "requirements": {
            **checks,
            "gnome_shell_extension": ready,
            "background_semantic_actions": "provided by the separate computer-use@openai-bundled AT-SPI plugin",
        },
        "active_lease": load_lease() is not None,
        "safety_note": "Mutter exposes one global seat; lease input can visibly contend with physical input and cannot detect every held hardware button.",
    }


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2)}], "structuredContent": value, "isError": False}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "session_status":
        return text_result(status())
    if name == "list_session_windows":
        return text_result({"windows": windows(), "stable_id_lifetime": "until GNOME Shell or the window restarts"})
    if name == "capture_session_window":
        return capture_window(arguments)
    if name == "begin_focus_lease":
        with file_guard(LOCK_FILE):
            return text_result(begin_lease(arguments))
    if name in {"lease_pointer_click", "lease_pointer_scroll", "lease_pointer_drag"}:
        action = name.removeprefix("lease_pointer_")
        return text_result(pointer_action(arguments, action))
    if name == "send_lease_shortcut":
        return text_result(send_shortcut(arguments))
    if name == "end_focus_lease":
        with file_guard(LOCK_FILE):
            with INPUT_LOCK, file_guard(INPUT_FILE):
                return text_result(restore_lease(require_lease(str(arguments["lease_token"]))))
    if name == "recover_focus_lease":
        with file_guard(LOCK_FILE):
            with INPUT_LOCK, file_guard(INPUT_FILE):
                state = load_lease()
                return text_result({"restored": True, "message": "no unfinished focus lease"} if not state else restore_lease(state, recovery=True))
    raise ValueError(f"unknown tool: {name}")


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": "Operate the real GNOME session. Prefer the separate Computer Use plugin's AT-SPI tools; global-seat actions require an acknowledged, journaled focus lease.",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            result = call_tool(str(params.get("name") or ""), arguments)
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def main() -> int:
    write_lock = threading.Lock()
    workers: list[threading.Thread] = []

    def process(message: dict[str, Any]) -> None:
        response = dispatch(message)
        if response is not None:
            with write_lock:
                print(json.dumps(response, separators=(",", ":")), flush=True)

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except Exception as exc:
            with write_lock:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}), flush=True)
            continue
        worker = threading.Thread(target=process, args=(message,), daemon=True)
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    return 0
