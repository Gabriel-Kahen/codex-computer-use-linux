import base64
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import focus_lease
from . import kwin


SERVER_INFO = {"name": "plasma-same-session-computer-use", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-11-25"
# Base64 expansion must stay below the rmcp client's 8 MiB stdio line cap.
MAX_CAPTURE_BYTES = 5 * 1024 * 1024
MAX_LIST_WINDOWS = 10
MAX_WINDOW_ID_CHARS = 80
MAX_WINDOW_TITLE_CHARS = 160
MAX_WINDOW_CLASS_CHARS = 96

TOOLS = [
    {
        "name": "plasma_session_status",
        "description": "Report detected KWin/Plasma capabilities and their runtime requirements without claiming unavailable background input.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_plasma_windows",
        "description": "List a bounded page of windows in the current KWin session with stable internal UUIDs, desktops, processes, and exact-capture identifiers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_WINDOWS, "default": 10},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "capture_plasma_window",
        "description": "Capture one exact KWin window by UUID, class, or title using KWin's compositor-side ScreenShot2 API without changing focus, desktop, or pointer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string"},
                "save_path": {"type": ["string", "null"], "description": "Optional absolute PNG path."},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "begin_plasma_focus_lease",
        "description": "Acknowledge interference, journal restorable KWin state, and activate an existing window. This prepares focus but cannot authorize, scope, or gate a separate global-input tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string"},
                "acknowledge_interference": {"type": "boolean", "description": "Must be true because Plasma has one shared input seat."},
                "max_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60, "description": "Advisory recovery deadline; it does not disable external input."},
            },
            "required": ["window", "acknowledge_interference"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "validate_plasma_focus_lease",
        "description": "Recheck the advisory deadline, lock state, live target, and active KWin focus. Call immediately before each separate global-input action; a ready result is advisory and does not gate that external tool.",
        "inputSchema": {"type": "object", "properties": {"lease_token": {"type": "string"}}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "end_plasma_focus_lease",
        "description": "Restore the target's desktop and the user's original KWin desktop/focus. Reports the pointer coordinate that the companion Computer Use tool must restore if global pointer input moved it.",
        "inputSchema": {"type": "object", "properties": {"lease_token": {"type": "string"}}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recover_plasma_focus_lease",
        "description": "Restore KWin state from an unfinished focus/restoration lease after interruption or an advisory deadline.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]




def _window_for_model(window: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(window)
    bounded["id"] = str(window.get("id") or "")[:MAX_WINDOW_ID_CHARS]
    bounded["capture_id"] = str(window.get("capture_id") or "")[:MAX_WINDOW_ID_CHARS]
    bounded["title"] = str(window.get("title") or "")[:MAX_WINDOW_TITLE_CHARS]
    bounded["class"] = str(window.get("class") or "")[:MAX_WINDOW_CLASS_CHARS]
    return bounded


def capture_result(arguments: dict[str, Any]) -> dict[str, Any]:
    focus_lease._require_unlocked("window capture")
    selected = kwin.resolve_window(str(arguments["window"]))
    if selected.get("excluded_from_capture") is True:
        raise RuntimeError("the selected application asked KWin to exclude this window from capture")
    requested = arguments.get("save_path")
    if requested:
        output = Path(str(requested)).expanduser()
        if not output.is_absolute():
            raise ValueError("save_path must be absolute")
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    else:
        output = None
        fd, name = tempfile.mkstemp(prefix="plasma-window-", suffix=".png")
    os.close(fd)
    temporary = Path(name)
    try:
        focus_lease._require_unlocked("window capture")
        before = {"focus": kwin.active_window_id(), "desktop": kwin.current_desktop(), "pointer": kwin.pointer_position()}
        kwin.capture_window(selected["id"], temporary)
        raw = temporary.read_bytes()
        if len(raw) > MAX_CAPTURE_BYTES:
            raise RuntimeError(f"captured PNG exceeds the {MAX_CAPTURE_BYTES}-byte safety limit")
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("KWin capture helper did not produce a PNG image")
        after = {"focus": kwin.active_window_id(), "desktop": kwin.current_desktop(), "pointer": kwin.pointer_position()}
        if output:
            temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = {
        "window": _window_for_model(selected),
        "saved_to": str(output) if output else None,
        "compositor_capture": "org.kde.KWin.ScreenShot2.CaptureWindow",
        "observed_physical_state_unchanged": before == after,
        "physical_state_before": before,
        "physical_state_after": after,
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps(metadata, indent=2)},
            {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"},
        ],
        "isError": False,
    }


def session_status() -> dict[str, Any]:
    requirements = kwin.helper_requirements()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    plasma = "KDE" in desktop.upper() or "PLASMA" in desktop.upper()
    wayland = bool(os.environ.get("WAYLAND_DISPLAY")) and os.environ.get("XDG_SESSION_TYPE", "wayland").lower() == "wayland"
    plasma_wayland = plasma and wayland
    kwin_service = False
    screenshot_interface = False
    if requirements["gdbus"]:
        probe = kwin.run(["gdbus", "introspect", "--session", "--dest", "org.kde.KWin", "--object-path", "/KWin"])
        kwin_service = probe.returncode == 0
        capture_probe = kwin.run(["gdbus", "introspect", "--session", "--dest", "org.kde.KWin.ScreenShot2", "--object-path", "/org/kde/KWin/ScreenShot2"])
        screenshot_interface = capture_probe.returncode == 0 and "CaptureWindow" in capture_probe.stdout
    capture_buildable = all(
        requirements[name]
        for name in ("kdotool", "cxx", "pkg_config", "qt6_development_files", "capture_helper_source")
    )
    return {
        "session": "real-current-login",
        "backend": "kwin-wayland",
        "plasma_session_detected": plasma,
        "plasma_wayland_session_detected": plasma_wayland,
        "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        "session_locked": kwin.screen_locked() if requirements["gdbus"] else None,
        "capabilities": {
            "stable_window_ids": plasma_wayland and requirements["kdotool"] and kwin_service,
            "exact_capture_transport_available": plasma_wayland and screenshot_interface and capture_buildable,
            "exact_background_window_capture": plasma_wayland and screenshot_interface and capture_buildable and kwin.capture_authorized_in_current_session(),
            "background_semantic_actions": False,
            "targeted_background_keyboard": False,
            "targeted_background_pointer": False,
            "acknowledged_focus_restoration_lease": (
                plasma_wayland and requirements["kdotool"] and requirements["qdbus"] and kwin_service
            ),
            "external_global_input_gated_by_broker": False,
            "automatic_pointer_restore": False,
        },
        "requirements": requirements,
        "kwin_dbus_service": kwin_service,
        "kwin_screenshot_interface": screenshot_interface,
        "notes": {
            "semantic_actions": "provided by the separate Computer Use plugin over AT-SPI",
            "input": (
                "KWin has no stable public API for arbitrary per-window input. The broker only prepares and "
                "revalidates focus/restoration state; it cannot authorize, scope, or gate a separate global-input tool."
            ),
            "capture_authorization": "exact_background_window_capture becomes true only after this helper completes an authorized capture; exact_capture_transport_available means a first attempt is possible",
        },
    }


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}], "structuredContent": value, "isError": False}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "plasma_session_status":
        return text_result(session_status())
    if name == "list_plasma_windows":
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 10)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_WINDOWS:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_WINDOWS}")
        windows = kwin.list_windows()
        end = min(offset + limit, len(windows))
        return text_result({
            "windows": [_window_for_model(window) for window in windows[offset:end]],
            "total": len(windows),
            "next_offset": end if end < len(windows) else None,
        })
    if name == "capture_plasma_window":
        return capture_result(arguments)
    with kwin.file_guard(focus_lease.LEASE_LOCK):
        if name == "begin_plasma_focus_lease":
            return text_result(focus_lease.begin_lease(arguments))
        if name == "validate_plasma_focus_lease":
            state = focus_lease._require_lease(str(arguments["lease_token"]))
            return text_result(focus_lease.validate_focus_lease(state))
        if name == "end_plasma_focus_lease":
            return text_result(focus_lease._restore(focus_lease._require_lease(str(arguments["lease_token"]))))
        if name == "recover_plasma_focus_lease":
            state = focus_lease._load_lease()
            return text_result(
                {"restored": True, "recovery_complete": True, "message": "no unfinished Plasma focus lease"}
                if not state
                else focus_lease._restore(state)
            )
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
                "instructions": (
                    "Operate the user's real Plasma session. Prefer the separate Computer Use plugin's AT-SPI tools. "
                    "A focus lease only journals/restores KWin state and provides advisory validation; it cannot gate "
                    "the separate plugin's global input."
                ),
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
    output_lock = threading.Lock()
    workers = []

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
        worker = threading.Thread(target=process, args=(message,), daemon=True)
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join()
    return 0
