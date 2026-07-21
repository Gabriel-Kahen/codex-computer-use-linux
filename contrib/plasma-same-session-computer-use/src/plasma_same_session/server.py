import base64
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import coordination
from . import focus_lease
from . import kwin


SERVER_INFO = {"name": "plasma-same-session-computer-use", "version": "0.2.0"}
PROTOCOL_VERSION = "2025-11-25"
# Base64 expansion must stay below the rmcp client's 8 MiB stdio line cap.
MAX_CAPTURE_BYTES = 5 * 1024 * 1024
MAX_LIST_WINDOWS = 10
MAX_WINDOW_LIST_BYTES = 4 * 1024
MAX_ERROR_TEXT_CHARS = 1000
MAX_ERROR_TEXT_BYTES = 4 * 1024
MAX_STATUS_TEXT_CHARS = 128
MAX_STATUS_TEXT_BYTES = 512
MAX_WINDOW_ID_CHARS = coordination.MAX_WINDOW_ID_CHARS
MAX_WINDOW_TITLE_CHARS = coordination.MAX_WINDOW_TITLE_CHARS
MAX_WINDOW_CLASS_CHARS = coordination.MAX_WINDOW_CLASS_CHARS
MAX_LIST_CLAIMS = coordination.MAX_CLAIMS_PER_PAGE
MAX_CLAIM_PAGE_OFFSET = coordination.MAX_ACTIVE_CLAIMS
MAX_SAVE_PATH_CHARS = 4096
MAX_SAVE_PATH_BYTES = 4 * 1024
MAX_CAPTURE_METADATA_BYTES = 16 * 1024
CLAIM_TOKEN_SCHEMA = {
    "type": ["string", "null"],
    "maxLength": coordination.MAX_CLAIM_TOKEN_CHARS,
    "pattern": f"^{coordination.CLAIM_TOKEN_PATTERN}$",
}
LEASE_TOKEN_SCHEMA = {
    "type": "string",
    "maxLength": focus_lease.MAX_LEASE_TOKEN_CHARS,
    "pattern": f"^{focus_lease.LEASE_TOKEN_PATTERN}$",
}
WINDOW_SCHEMA = {"type": "string", "maxLength": coordination.MAX_WINDOW_QUERY_CHARS}


def bounded_text(value: Any, max_chars: int, max_bytes: int) -> str:
    text = str(value)[:max_chars]
    if len(json.dumps(text, ensure_ascii=False).encode()) - 2 <= max_bytes:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle], ensure_ascii=False).encode()) - 2 <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def physical_state() -> dict[str, Any]:
    active_window = kwin.active_window_id()
    if active_window is not None:
        active_window = coordination.require_window_id(active_window)
    return {
        "focus": active_window,
        "desktop": coordination.optional_desktop(kwin.current_desktop(), "physical desktop"),
        "pointer": coordination.optional_pointer(kwin.pointer_position()),
    }

TOOLS = [
    {
        "name": "plasma_session_status",
        "description": "Report detected KWin/Plasma capabilities and their runtime requirements without claiming unavailable background input.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_plasma_windows",
        "description": "List a bounded compact page of windows in the current KWin session with stable internal UUIDs and exact-capture identifiers.",
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
        "description": "Deprecated compatibility tool. Capture one exact KWin window and optionally write save_path. Prefer get_plasma_window_capture for inline capture or save_plasma_window_capture for writes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": WINDOW_SCHEMA,
                "save_path": {"type": ["string", "null"], "maxLength": MAX_SAVE_PATH_CHARS, "description": "Optional absolute PNG path."},
                "claim_token": {**CLAIM_TOKEN_SCHEMA, "description": "Required when the selected window has an active claim."},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "get_plasma_window_capture",
        "description": "Return an inline PNG of one exact KWin window without changing physical state or creating a caller-selected file. Different windows capture in parallel; an active foreign window claim is enforced.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": WINDOW_SCHEMA,
                "claim_token": {**CLAIM_TOKEN_SCHEMA, "description": "Required when the selected window has an active claim."},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "save_plasma_window_capture",
        "description": "Capture one exact KWin window and atomically create or replace an absolute PNG path. Also returns the PNG inline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": WINDOW_SCHEMA,
                "save_path": {"type": "string", "minLength": 1, "maxLength": MAX_SAVE_PATH_CHARS, "description": "Absolute PNG path to atomically create or replace after capture succeeds."},
                "claim_token": {**CLAIM_TOKEN_SCHEMA, "description": "Required when the selected window has an active claim."},
            },
            "required": ["window", "save_path"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "claim_session_window",
        "description": "Claim one KWin window for this Codex thread. Claims are cross-process, expire after a bounded TTL, and do not block claims or captures for other windows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": WINDOW_SCHEMA,
                "lease_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60},
                "claim_token": {**CLAIM_TOKEN_SCHEMA, "description": "Required to renew an active claim, including one owned by this thread."},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "release_session_window",
        "description": "Release this Codex thread's window claim. A different thread may only recover an expired claim or one whose broker process exited.",
        "inputSchema": {
            "type": "object",
            "properties": {"claim_token": {**CLAIM_TOKEN_SCHEMA, "type": "string"}},
            "required": ["claim_token"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_window_claims",
        "description": "List a bounded page of live window claims and their owner thread IDs without exposing claim tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {"type": "integer", "minimum": 0, "maximum": MAX_CLAIM_PAGE_OFFSET, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_CLAIMS, "default": 20},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "begin_plasma_focus_lease",
        "description": "Acquire the broker's one global-seat lease, bind it to this thread and window claim, journal restorable KWin state, and activate the window. External input remains advisory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": WINDOW_SCHEMA,
                "acknowledge_interference": {"type": "boolean", "description": "Must be true because Plasma has one shared input seat."},
                "max_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60, "description": "Advisory recovery deadline; it does not disable external input."},
                "claim_token": {**CLAIM_TOKEN_SCHEMA, "description": "Required for an existing claim. An unclaimed window receives a new implicit claim."},
            },
            "required": ["window", "acknowledge_interference"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "validate_plasma_focus_lease",
        "description": "Recheck this thread's global-seat lease, bound window claim, deadline, lock state, live target, and KWin focus immediately before external global input.",
        "inputSchema": {"type": "object", "properties": {"lease_token": LEASE_TOKEN_SCHEMA}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "end_plasma_focus_lease",
        "description": "Restore the target's desktop and the user's original KWin desktop/focus. Reports the pointer coordinate that the companion Computer Use tool must restore if global pointer input moved it.",
        "inputSchema": {"type": "object", "properties": {"lease_token": LEASE_TOKEN_SCHEMA}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recover_plasma_focus_lease",
        "description": "Recover KWin state from this thread's lease. A different thread may recover only after expiry, broker-process exit, or a KWin session change.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def capture_result(arguments: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
    owner_id = owner_id or coordination.legacy_owner_id()
    focus_lease.require_unlocked("window capture")
    selected = kwin.resolve_window(coordination.require_window_query(arguments.get("window")))
    if selected.get("excluded_from_capture") is True:
        raise RuntimeError("the selected application asked KWin to exclude this window from capture")
    claim_token = arguments.get("claim_token")
    if claim_token is not None:
        claim_token = coordination.require_claim_token(claim_token)
    with coordination.window_action(selected["id"], owner_id, claim_token):
        requested = arguments.get("save_path")
        if requested == "":
            requested = None
        if requested is not None:
            if (
                not isinstance(requested, str)
                or not requested
                or len(requested) > MAX_SAVE_PATH_CHARS
                or len(requested.encode()) > MAX_SAVE_PATH_BYTES
            ):
                raise ValueError("save_path is invalid")
            if any(ord(character) < 32 or ord(character) == 127 for character in requested):
                raise ValueError("save_path is invalid")
            output = Path(requested).expanduser()
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
            focus_lease.require_unlocked("window capture")
            before = physical_state()
            kwin.capture_window(selected["id"], temporary)
            if temporary.stat().st_size > MAX_CAPTURE_BYTES:
                raise RuntimeError(f"captured PNG exceeds the {MAX_CAPTURE_BYTES}-byte safety limit")
            with temporary.open("rb") as image:
                raw = image.read(MAX_CAPTURE_BYTES + 1)
            if len(raw) > MAX_CAPTURE_BYTES:
                raise RuntimeError(f"captured PNG exceeds the {MAX_CAPTURE_BYTES}-byte safety limit")
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("KWin capture helper did not produce a PNG image")
            after = physical_state()
            metadata = {
                "window": coordination.window_for_model(selected),
                "saved_to": str(output) if output else None,
                "compositor_capture": "org.kde.KWin.ScreenShot2.CaptureWindow",
                "observed_physical_state_unchanged": before == after,
                "physical_state_before": before,
                "physical_state_after": after,
                "window_claim_enforced": True,
            }
            metadata_text = json.dumps(metadata, indent=2)
            if len(metadata_text.encode()) > MAX_CAPTURE_METADATA_BYTES:
                raise RuntimeError("capture metadata exceeds its serialized size limit")
            if output:
                temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "content": [
                {"type": "text", "text": metadata_text},
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
        "wayland_display": (
            bounded_text(
                os.environ.get("WAYLAND_DISPLAY"),
                MAX_STATUS_TEXT_CHARS,
                MAX_STATUS_TEXT_BYTES,
            )
            if os.environ.get("WAYLAND_DISPLAY") is not None
            else None
        ),
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
            "cross_process_window_claims": plasma_wayland and requirements["kdotool"] and kwin_service,
            "parallel_exact_window_capture": plasma_wayland and screenshot_interface and capture_buildable,
            "serialized_global_seat_focus_leases": (
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
                "KWin has no stable public API for arbitrary per-window input. The broker enforces cross-process "
                "window ownership and one global-seat focus lease, but it cannot gate a separate global-input tool."
            ),
            "parallelism": "exact capture actions use independent per-window locks; claim bookkeeping is brief and only focus/global-seat work is serialized for the action duration",
            "capture_authorization": "exact_background_window_capture becomes true only after this helper completes an authorized capture; exact_capture_transport_available means a first attempt is possible",
        },
    }


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}], "structuredContent": value, "isError": False}


def call_tool(name: str, arguments: dict[str, Any], *, thread_id: str | None = None) -> dict[str, Any]:
    owner_id = thread_id or coordination.legacy_owner_id()
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
        page: list[dict[str, Any]] = []
        for window in windows[offset : offset + limit]:
            candidate = [*page, coordination.window_for_model(window)]
            end = offset + len(candidate)
            value = {
                "windows": candidate,
                "total": len(windows),
                "next_offset": end if end < len(windows) else None,
            }
            if coordination.serialized_size(value) > MAX_WINDOW_LIST_BYTES:
                if not page:
                    raise RuntimeError("a Plasma window exceeds the serialized list size limit")
                break
            page = candidate
        end = offset + len(page)
        return text_result({
            "windows": page,
            "total": len(windows),
            "next_offset": end if end < len(windows) else None,
        })
    if name in {"capture_plasma_window", "get_plasma_window_capture", "save_plasma_window_capture"}:
        if name == "get_plasma_window_capture" and arguments.get("save_path") not in (None, ""):
            raise ValueError(
                "get_plasma_window_capture does not accept save_path; use save_plasma_window_capture to write a PNG"
            )
        if name == "save_plasma_window_capture" and (
            not isinstance(arguments.get("save_path"), str)
            or not arguments["save_path"]
        ):
            raise ValueError("save_path must be a non-empty string")
        return capture_result(arguments, owner_id)
    if name == "claim_session_window":
        owner_id = coordination.require_thread_id(thread_id)
        selected = kwin.resolve_window(coordination.require_window_query(arguments.get("window")))
        return text_result(coordination.claim_window(
            coordination.window_for_model(selected),
            owner_id,
            arguments.get("lease_seconds", 60),
            arguments.get("claim_token"),
        ))
    if name == "release_session_window":
        owner_id = coordination.require_thread_id(thread_id)
        return text_result(coordination.release_window_claim(
            coordination.require_claim_token(arguments.get("claim_token")),
            owner_id,
        ))
    if name == "list_window_claims":
        owner_id = coordination.require_thread_id(thread_id)
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 20)
        if type(offset) is not int or not 0 <= offset <= MAX_CLAIM_PAGE_OFFSET:
            raise ValueError(f"offset must be between 0 and {MAX_CLAIM_PAGE_OFFSET}")
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_CLAIMS:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_CLAIMS}")
        return text_result(coordination.list_claims(owner_id, offset, limit))
    with kwin.file_guard(focus_lease.LEASE_LOCK):
        if name == "begin_plasma_focus_lease":
            return text_result(focus_lease.begin(arguments, owner_id))
        if name == "validate_plasma_focus_lease":
            state = focus_lease.require(focus_lease.require_lease_token(arguments.get("lease_token")), owner_id)
            if state.get("owner"):
                state["owner"]["process"] = coordination.current_process_identity()
                focus_lease.save(state)
            return text_result(focus_lease.validate(state, owner_id))
        if name == "end_plasma_focus_lease":
            state = focus_lease.require(
                focus_lease.require_lease_token(arguments.get("lease_token")),
                owner_id,
                allow_recovery=True,
            )
            return text_result(focus_lease.restore(state))
        if name == "recover_plasma_focus_lease":
            state = focus_lease.load()
            if state:
                focus_lease.require(focus_lease.require_lease_token(state.get("token")), owner_id, allow_recovery=True)
            return text_result(
                {"restored": True, "recovery_complete": True, "message": "no unfinished Plasma focus lease"}
                if not state
                else focus_lease.restore(state)
            )
    raise ValueError(
        "unknown tool: "
        + bounded_text(name, MAX_STATUS_TEXT_CHARS, MAX_STATUS_TEXT_BYTES)
    )


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
                    "Claim each window before assigning it to a parallel agent. Exact captures of different windows "
                    "run concurrently. Focus leases are owner-bound and serialize the shared seat, but validation "
                    "cannot gate the separate plugin's global input."
                ),
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("tool call params must be an object")
            metadata = params.get("_meta") or {}
            if not isinstance(metadata, dict):
                raise ValueError("tool call _meta must be an object")
            thread_id = coordination.parse_thread_id(metadata.get("threadId"))
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            result = call_tool(str(params.get("name") or ""), arguments, thread_id=thread_id)
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "method not found: "
                    + bounded_text(method, MAX_STATUS_TEXT_CHARS, MAX_STATUS_TEXT_BYTES),
                },
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": bounded_text(exc, MAX_ERROR_TEXT_CHARS, MAX_ERROR_TEXT_BYTES),
            },
        }


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
                print(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": bounded_text(
                                exc,
                                MAX_ERROR_TEXT_CHARS,
                                MAX_ERROR_TEXT_BYTES,
                            ),
                        },
                    }),
                    flush=True,
                )
            continue
        worker = threading.Thread(target=process, args=(message,), daemon=True)
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join()
    return 0
