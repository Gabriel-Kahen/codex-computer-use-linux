import base64
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import kwin


SERVER_INFO = {"name": "plasma-same-session-computer-use", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-11-25"
LEASE_FILE = kwin.STATE_DIR / "focus-lease.json"
LEASE_LOCK = kwin.STATE_DIR / "focus-lease.lock"

TOOLS = [
    {
        "name": "plasma_session_status",
        "description": "Report detected KWin/Plasma capabilities and their runtime requirements without claiming unavailable background input.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_plasma_windows",
        "description": "List windows in the current KWin session with stable internal UUIDs, desktops, processes, and exact-capture identifiers.",
        "inputSchema": {"type": "object", "properties": {}},
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


def _load_lease() -> dict[str, Any] | None:
    if not LEASE_FILE.exists():
        return None
    LEASE_FILE.parent.chmod(0o700)
    LEASE_FILE.chmod(0o600)
    return json.loads(LEASE_FILE.read_text())


def _save_lease(state: dict[str, Any]) -> None:
    LEASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEASE_FILE.parent.chmod(0o700)
    temporary = LEASE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.chmod(0o600)
    temporary.replace(LEASE_FILE)


def _require_lease(token: str) -> dict[str, Any]:
    state = _load_lease()
    if not state:
        raise RuntimeError("no Plasma focus/restoration lease exists")
    if not secrets.compare_digest(str(state.get("token") or ""), token):
        raise ValueError("lease token does not match the Plasma focus/restoration journal")
    return state


def _restore(state: dict[str, Any]) -> dict[str, Any]:
    original = state.get("original") or {}
    target = state.get("target") or {}
    errors: list[str] = []
    missing_windows: list[str] = []
    verified: dict[str, bool] = {}
    try:
        live_ids = {window["id"] for window in kwin.list_windows()}
    except Exception as exc:
        errors.append(f"window enumeration: {exc}")
        live_ids = set()
    target_id = str(target.get("id") or "")
    target_desktop = original.get("target_desktop")
    if not target_id:
        errors.append("restoration journal has no target window id")
    elif target_id not in live_ids and not errors:
        missing_windows.append(f"target:{target_id}")
    elif target_id in live_ids and target_desktop is not None:
        try:
            kwin.set_window_desktop(target_id, int(target_desktop))
            observed_desktop = kwin.window_desktop(target_id)
            verified["target_desktop"] = observed_desktop == int(target_desktop)
            if not verified["target_desktop"]:
                errors.append(f"target desktop verification: expected {target_desktop}, observed {observed_desktop}")
        except Exception as exc:
            errors.append(f"target desktop restore: {exc}")
        if original.get("target_minimized") is not None:
            try:
                kwin.set_window_minimized(target_id, bool(original["target_minimized"]))
                observed_minimized = kwin.window_boolean(target_id, "minimized")
                verified["target_minimized"] = observed_minimized is bool(original["target_minimized"])
                if not verified["target_minimized"]:
                    errors.append(
                        "target minimized-state verification: "
                        f"expected {original['target_minimized']}, observed {observed_minimized}"
                    )
            except Exception as exc:
                errors.append(f"target minimized-state restore: {exc}")
    if original.get("desktop") is not None:
        try:
            kwin.set_desktop(int(original["desktop"]))
            observed_desktop = kwin.current_desktop()
            verified["desktop"] = observed_desktop == int(original["desktop"])
            if not verified["desktop"]:
                errors.append(f"desktop verification: expected {original['desktop']}, observed {observed_desktop}")
        except Exception as exc:
            errors.append(f"desktop restore: {exc}")
    active_id = str(original.get("active_window") or "")
    if not active_id:
        errors.append("restoration journal has no original active window id")
    elif active_id not in live_ids and not errors:
        missing_windows.append(f"original-active:{active_id}")
    elif active_id in live_ids:
        try:
            kwin.activate(active_id)
            observed_active = kwin.active_window_id()
            verified["focus"] = observed_active == active_id
            if not verified["focus"]:
                errors.append(f"focus verification: expected {active_id}, observed {observed_active}")
        except Exception as exc:
            errors.append(f"focus restore: {exc}")
    original_pointer = original.get("pointer")
    observed_pointer = None
    if original_pointer is not None:
        try:
            observed_pointer = kwin.pointer_position()
            verified["pointer"] = observed_pointer == original_pointer
            if not verified["pointer"]:
                errors.append(f"pointer verification: expected {original_pointer}, observed {observed_pointer}")
        except Exception as exc:
            errors.append(f"pointer verification: {exc}")
    recovery_complete = not errors
    if recovery_complete:
        LEASE_FILE.unlink(missing_ok=True)
    return {
        "restored": recovery_complete and not missing_windows,
        "recovery_complete": recovery_complete,
        "errors": errors,
        "missing_windows": missing_windows,
        "verified": verified,
        "journal_retained": not recovery_complete,
        "focus_restored_to": active_id if verified.get("focus") else None,
        "desktop_restored_to": original.get("desktop") if verified.get("desktop") else None,
        "target_desktop_restored_to": target_desktop if verified.get("target_desktop") else None,
        "requested_focus_restore": active_id or None,
        "requested_desktop_restore": original.get("desktop"),
        "requested_target_desktop_restore": target_desktop,
        "pointer_restore_required": original_pointer is not None and not verified.get("pointer", False),
        "pointer_restore_coordinate": original_pointer,
        "observed_pointer": observed_pointer,
        "pointer_restored_by_this_backend": False,
    }


def begin_lease(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true; Plasma exposes one shared input seat")
    locked = kwin.screen_locked()
    if locked is not False:
        state = "locked" if locked else "could not be verified as unlocked"
        raise RuntimeError(f"focus lease refused because the Plasma session is {state}")
    existing = _load_lease()
    if existing and time.time() > int(existing.get("expires_at") or 0):
        restored = _restore(existing)
        if not restored["recovery_complete"]:
            raise RuntimeError(f"expired focus lease could not be recovered: {restored['errors']}")
    elif existing:
        raise RuntimeError("a Plasma focus/restoration lease already exists; end or recover it first")
    max_seconds = int(arguments.get("max_seconds", 60))
    if not 5 <= max_seconds <= 300:
        raise ValueError("max_seconds must be between 5 and 300")
    target = kwin.resolve_window(str(arguments["window"]))
    if target.get("desktop") is None:
        raise RuntimeError("focus lease refused because the target desktop could not be queried")
    if target.get("minimized") is None:
        raise RuntimeError("focus lease refused because KWin minimized state could not be read through qdbus")
    pointer = kwin.pointer_position()
    if pointer is None:
        raise RuntimeError("focus lease refused because the physical pointer position could not be journaled")
    original_active = kwin.active_window_id()
    if original_active is None or not kwin.window_info(original_active):
        raise RuntimeError("focus lease refused because the original active window could not be positively identified")
    token = secrets.token_urlsafe(18)
    state = {
        "version": 1,
        "token": token,
        "phase": "prepared",
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + max_seconds,
        "target": target,
        "original": {
            "active_window": original_active,
            "desktop": kwin.current_desktop(),
            "target_desktop": target["desktop"],
            "target_minimized": target["minimized"],
            "pointer": pointer,
        },
    }
    _save_lease(state)
    try:
        kwin.activate(target["id"])
        observed_active = kwin.active_window_id()
        if observed_active != target["id"]:
            raise RuntimeError(f"KWin did not activate the target; observed active window {observed_active}")
        state["phase"] = "active"
        _save_lease(state)
    except Exception as exc:
        restored = _restore(state)
        suffix = "" if restored["recovery_complete"] else f"; restoration also failed: {restored['errors']}"
        raise RuntimeError(f"focus lease activation failed: {exc}{suffix}") from exc
    return {
        "lease_token": token,
        "expires_at": state["expires_at"],
        "window": target,
        "pointer_before": state["original"]["pointer"],
        "next_step": "Call validate_plasma_focus_lease immediately before each separate global-input action. This broker cannot enforce that requirement or gate the external tool.",
        "interference_boundary": "focus, workspace, keyboard, and pointer are shared with the physical Plasma session",
        "external_input_gated_by_broker": False,
        "token_scope": "access to this broker's restoration journal only",
        "deadline_scope": "advisory revalidation and recovery only; it does not disable external input",
    }


def validate_focus_lease(state: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    target_id = str((state.get("target") or {}).get("id") or "")
    try:
        live_ids = {window["id"] for window in kwin.list_windows()}
    except Exception as exc:
        errors.append(f"window enumeration: {exc}")
        live_ids = set()
    locked = kwin.screen_locked()
    active_id = kwin.active_window_id()
    expired = time.time() >= int(state.get("expires_at") or 0)
    target_live = target_id in live_ids
    target_active = bool(target_id) and active_id == target_id
    advisory_ready = (
        state.get("phase") == "active"
        and not expired
        and locked is False
        and target_live
        and target_active
        and not errors
    )
    return {
        "advisory_ready": advisory_ready,
        "phase": state.get("phase"),
        "expired": expired,
        "session_locked": locked,
        "target_live": target_live,
        "target_active": target_active,
        "observed_active_window": active_id,
        "errors": errors,
        "external_input_gated_by_broker": False,
        "required_caller_action": (
            "Revalidate immediately before every companion global-input action. "
            "Do not invoke it when advisory_ready is false. This is caller policy, not broker enforcement."
        ),
    }


def capture_result(arguments: dict[str, Any]) -> dict[str, Any]:
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
        before = {"focus": kwin.active_window_id(), "desktop": kwin.current_desktop(), "pointer": kwin.pointer_position()}
        kwin.capture_window(selected["id"], temporary)
        raw = temporary.read_bytes()
        after = {"focus": kwin.active_window_id(), "desktop": kwin.current_desktop(), "pointer": kwin.pointer_position()}
        if output:
            temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = {
        "window": selected,
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
        "structuredContent": metadata,
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
        return text_result({"windows": kwin.list_windows()})
    if name == "capture_plasma_window":
        return capture_result(arguments)
    with kwin.file_guard(LEASE_LOCK):
        if name == "begin_plasma_focus_lease":
            return text_result(begin_lease(arguments))
        if name == "validate_plasma_focus_lease":
            state = _require_lease(str(arguments["lease_token"]))
            return text_result(validate_focus_lease(state))
        if name == "end_plasma_focus_lease":
            return text_result(_restore(_require_lease(str(arguments["lease_token"]))))
        if name == "recover_plasma_focus_lease":
            state = _load_lease()
            return text_result(
                {"restored": True, "recovery_complete": True, "message": "no unfinished Plasma focus lease"}
                if not state
                else _restore(state)
            )
    raise ValueError(f"unknown tool: {name}")


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        method = message.get("method")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
            result = {
                "protocolVersion": requested,
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
