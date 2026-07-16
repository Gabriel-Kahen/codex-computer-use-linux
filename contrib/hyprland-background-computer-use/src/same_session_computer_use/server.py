import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import coordination
from .native_plugin import STATE_DIR
from .native_plugin import ensure_native_input_safe
from .native_plugin import ensure_target_pointer_plugin
from .native_plugin import file_guard
from .native_plugin import plugin_build_requirements


SERVER_INFO = {"name": "same-session-computer-use", "version": "0.2.0"}
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", PROTOCOL_VERSION})
# Base64 expansion must stay below the rmcp client's 8 MiB stdio line cap.
MAX_CAPTURE_PNG_BYTES = 5 * 1024 * 1024
MAX_CAPTURE_PIXELS = 7680 * 4320
MAX_ERROR_TEXT_CHARS = 2048
MAX_ERROR_ITEMS = 8
MAX_WINDOW_RESULT_BYTES = 32 * 1024
# Keep each duplicated text/structured claim page below the 1k-token review threshold.
MAX_CLAIM_RESULT_BYTES = 2 * 1024
MAX_WINDOWS_PER_PAGE = 20
MAX_CLAIMS_PER_PAGE = 20
MAX_WINDOW_TEXT_CHARS = 512
# These two paths are retained as the migration source and global migration lock.
LEASE_FILE = STATE_DIR / "coordinate-lease.json"
LOCK_FILE = STATE_DIR / "coordinate-lease.lock"
_SESSION_ATTACHED = False
_SESSION_ENV_LOCK = threading.Lock()
_LEASE_GUARD_LOCAL = threading.local()

TOOLS = [
    {
        "name": "session_status",
        "description": "Check whether the real logged-in Hyprland session supports exact background window capture and targeted shortcuts.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_session_windows",
        "description": "List a bounded page of real windows from the user's current Hyprland login, including workspace, process, accessibility hints, and exact-capture identifiers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": ["string", "null"], "maxLength": 20},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_WINDOWS_PER_PAGE},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "claim_session_window",
        "description": "Exclusively claim one real window for this Codex task. Claims are fenced by the host-provided task identity, expire automatically, and are renewed by calling this tool again from the same task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS, "description": "Window address, exact-capture identifier, exact class, or title substring."},
                "lease_seconds": {"type": "integer", "minimum": 5, "maximum": 300, "default": 60},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "release_session_window",
        "description": "Release a live window claim owned by this Codex task. Releasing an already expired or released token is harmless.",
        "inputSchema": {
            "type": "object",
            "properties": {"claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH}},
            "required": ["claim_token"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_window_claims",
        "description": "List unexpired window claims for the active display and Hyprland instance. Fencing tokens are never disclosed by this tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": ["string", "null"], "maxLength": 20},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_CLAIMS_PER_PAGE},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "capture_session_window",
        "description": "Capture one exact real window without focusing it, moving it, changing workspace, or moving the pointer. Identify it by Hyprland address, exact-capture identifier, class, or title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS, "description": "Hyprland address, exact-capture identifier, exact class, or title substring."},
                "save_path": {"type": ["string", "null"], "description": "Optional absolute PNG path to atomically create or replace after capture succeeds."},
                "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."},
            },
            "required": ["window"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "send_window_shortcut",
        "description": "Send a key or shortcut directly to one real window by Hyprland address without focusing it. Prefer accessibility actions from the separate Computer Use plugin for buttons and text fields; use this for discrete shortcuts or characters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "minLength": 1, "maxLength": 64, "description": "Exact Hyprland window address from list_session_windows."},
                "key": {"type": "string", "description": "Hyprland key name, such as x, SPACE, RETURN, or XF86AudioPlay."},
                "modifiers": {"type": "string", "description": "Space-separated modifiers, such as CTRL SHIFT; empty for none.", "default": ""},
                "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."},
            },
            "required": ["address", "key"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "targeted_pointer_click",
        "description": "Click an exact coordinate inside a real Wayland or XWayland window without moving the physical cursor, changing the user's focused window, or switching workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."},
            },
            "required": ["window", "x", "y"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "targeted_pointer_scroll",
        "description": "Scroll at an exact coordinate inside a real Wayland or XWayland window without moving the physical cursor, changing focus, or switching workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS}, "x": {"type": "number"}, "y": {"type": "number"}, "steps": {"type": "integer", "minimum": -20, "maximum": 20}, "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."}},
            "required": ["window", "x", "y", "steps"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "targeted_pointer_drag",
        "description": "Drag between exact coordinates inside a real Wayland or XWayland window without moving the physical cursor, changing focus, or switching workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS}, "start_x": {"type": "number"}, "start_y": {"type": "number"}, "end_x": {"type": "number"}, "end_y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}, "motion_steps": {"type": "integer", "minimum": 2, "maximum": 32, "default": 8},
                "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."},
            },
            "required": ["window", "start_x", "start_y", "end_x", "end_y"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "begin_coordinate_lease",
        "description": "Move one existing real window to a temporary off-screen Hyprland output for coordinate-only Computer Use. Fullscreens the window on that fallback screen when needed, then restores its original fullscreen state, placement, physical focus, workspace, and pointer. This briefly takes global input focus and requires explicit acknowledgment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "minLength": 1, "maxLength": MAX_WINDOW_TEXT_CHARS, "description": "Window address, capture ID, exact class, or title substring."},
                "acknowledge_interference": {"type": "boolean", "description": "Must be true; raw pointer input can briefly contend with the user's physical input."},
                "fullscreen_if_needed": {"type": "boolean", "description": "Fullscreen the target on the temporary screen when it is not already fullscreen. Defaults to true.", "default": True},
                "claim_token": {"type": "string", "minLength": 1, "maxLength": coordination.MAX_CLAIM_TOKEN_LENGTH, "description": "Optional fencing token returned by claim_session_window."},
            },
            "required": ["window", "acknowledge_interference"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "capture_coordinate_desktop",
        "description": "Capture the temporary off-screen output for an active coordinate lease.",
        "inputSchema": {"type": "object", "properties": {"lease_token": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "end_coordinate_lease",
        "description": "Restore the leased real window's original fullscreen mode, workspace, physical focus, and pointer, then remove the temporary output.",
        "inputSchema": {"type": "object", "properties": {"lease_token": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["lease_token"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recover_coordinate_lease",
        "description": "Recover and restore compositor state from any unfinished coordinate lease after an interruption or crash.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def find_xwayland_display(instance: str, wayland_display: str, proc_root: Path = Path("/proc")) -> str | None:
    candidates: list[tuple[int, str]] = []
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid() or (process / "comm").read_text().strip() != "Xwayland":
                continue
            entries = (process / "environ").read_bytes().split(b"\0")
            environment = {
                key.decode(): value.decode()
                for entry in entries if b"=" in entry
                for key, value in [entry.split(b"=", 1)]
            }
        except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeDecodeError):
            continue
        if environment.get("HYPRLAND_INSTANCE_SIGNATURE") != instance:
            continue
        if environment.get("WAYLAND_DISPLAY") != wayland_display:
            continue
        display = environment.get("DISPLAY")
        if display:
            candidates.append((int(process.name), display))
    return max(candidates)[1] if candidates else None


def _attach_session_environment() -> None:
    global _SESSION_ATTACHED
    if _SESSION_ATTACHED:
        return
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        proc = subprocess.run(["hyprctl", "instances", "-j"], text=True, capture_output=True, timeout=5, check=False)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "failed to discover the active Hyprland session")
        instances = []
        for instance in json.loads(proc.stdout):
            process = Path(f"/proc/{instance.get('pid')}")
            if process.exists() and process.stat().st_uid == os.getuid():
                instances.append(instance)
        if not instances:
            raise RuntimeError("no live Hyprland session belongs to this login")
        wayland_display = os.environ.get("WAYLAND_DISPLAY")
        matching = [instance for instance in instances if instance.get("wl_socket") == wayland_display] if wayland_display else []
        selected = max(matching or instances, key=lambda instance: int(instance.get("time") or 0))
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = str(selected["instance"])
        os.environ.setdefault("WAYLAND_DISPLAY", str(selected["wl_socket"]))
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    xwayland_display = (
        find_xwayland_display(os.environ["HYPRLAND_INSTANCE_SIGNATURE"], wayland_display)
        if wayland_display else None
    )
    if xwayland_display:
        os.environ["DISPLAY"] = xwayland_display
    elif not os.environ.get("DISPLAY"):
        sockets = sorted(Path("/tmp/.X11-unix").glob("X*"))
        if len(sockets) == 1 and sockets[0].name[1:].isdigit():
            os.environ["DISPLAY"] = f":{sockets[0].name[1:]}"
    _SESSION_ATTACHED = True


def ensure_session_environment() -> None:
    if _SESSION_ATTACHED:
        return
    with _SESSION_ENV_LOCK:
        _attach_session_environment()


def session_binding() -> dict[str, Any]:
    ensure_session_environment()
    return {
        "uid": os.getuid(),
        "xdg_runtime_dir": os.environ.get("XDG_RUNTIME_DIR"),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        "hyprland_instance": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
    }


def hypr_windows() -> list[dict[str, Any]]:
    ensure_session_environment()
    proc = run(["hyprctl", "clients", "-j"])
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "failed to enumerate Hyprland windows")
    return json.loads(proc.stdout)


def combine_windows() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for win in hypr_windows():
        workspace = win.get("workspace") or {}
        result.append({
            "address": win.get("address"),
            "class": str(win.get("class") or ""),
            "title": str(win.get("title") or ""),
            "pid": win.get("pid"),
            "workspace": workspace.get("id"),
            "workspace_name": str(workspace.get("name") or ""),
            "monitor": win.get("monitor"),
            "focused": win.get("focusHistoryID") == 0,
            "mapped": win.get("mapped", True),
            "fullscreen": win.get("fullscreen", 0),
            "fullscreen_client": win.get("fullscreenClient", 0),
            "floating": win.get("floating", False),
            "xwayland": win.get("xwayland", False),
            "at": win.get("at"),
            "size": win.get("size"),
            "capture_id": str(win.get("stableId")) if win.get("stableId") is not None else None,
        })
    return result


def bounded_window(window: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(window)
    for field in ("address", "capture_id", "class", "title", "workspace_name"):
        if bounded.get(field) is not None:
            bounded[field] = str(bounded[field])[:MAX_WINDOW_TEXT_CHARS]
    return bounded


def bounded_error(value: object) -> str:
    message = str(value)
    if len(message) <= MAX_ERROR_TEXT_CHARS:
        return message
    return f"{message[:MAX_ERROR_TEXT_CHARS - 1]}…"


def resolve_window(query: str) -> dict[str, Any]:
    if not query or len(query) > MAX_WINDOW_TEXT_CHARS:
        raise ValueError(f"window must contain between 1 and {MAX_WINDOW_TEXT_CHARS} characters")
    windows = combine_windows()
    q = query.lower()
    exact = [w for w in windows if q in {str(w.get("address") or "").lower(), str(w.get("capture_id") or "").lower(), str(w.get("class") or "").lower()}]
    matches = exact or [w for w in windows if q in str(w.get("title") or "").lower()]
    if not matches:
        raise RuntimeError(f"no real session window matches {query!r}")
    if len(matches) > 1:
        choices = ", ".join(
            f"{str(w.get('class') or '')[:MAX_WINDOW_TEXT_CHARS]} "
            f"{str(w.get('title') or '')[:MAX_WINDOW_TEXT_CHARS]} "
            f"({str(w.get('address') or '')[:MAX_WINDOW_TEXT_CHARS]})"
            for w in matches[:8]
        )
        raise RuntimeError(f"window query is ambiguous; use an address or identifier: {choices}")
    if not matches[0].get("capture_id"):
        raise RuntimeError("the selected window has no exact-capture identifier")
    return matches[0]


def hypr_json(args: list[str]) -> Any:
    ensure_session_environment()
    proc = run(["hyprctl", "-j", *args])
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"hyprctl {' '.join(args)} failed")
    return json.loads(proc.stdout)


def hypr_dispatch(expression: str) -> None:
    ensure_session_environment()
    proc = run(["hyprctl", "dispatch", expression])
    if proc.returncode or "ok" not in proc.stdout.lower():
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"Hyprland dispatch failed: {expression}")


def lease_file(binding: dict[str, Any] | None = None) -> Path:
    key = coordination.binding_key(binding or session_binding())
    return LEASE_FILE.parent / "coordinate-leases" / key / "lease.json"


def lease_lock_file(binding: dict[str, Any] | None = None) -> Path:
    key = coordination.binding_key(binding or session_binding())
    return LOCK_FILE.parent / "coordinate-leases" / key / "lease.lock"


def _read_lease(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    path.parent.chmod(0o700)
    try:
        path.chmod(0o600)
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"coordinate lease state is unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("coordinate lease state has an unsupported format")
    return state


def legacy_lease_matches_current_session(state: dict[str, Any]) -> bool:
    """Identify an unbound 0.1.x journal from live compositor artifacts."""
    output = str(state.get("output") or "")
    if output and any(
        str(monitor.get("name") or "") == output
        for monitor in hypr_json(["monitors"])
    ):
        return True
    target = state.get("target") or {}
    target_pid = target.get("pid")
    if type(target_pid) is not int or target_pid <= 0:
        return False
    target_capture_id = str(target.get("capture_id") or "")
    target_address = str(target.get("address") or "")
    return any(
        window.get("pid") == target_pid
        and (
            (
                bool(target_capture_id)
                and str(window.get("capture_id") or "") == target_capture_id
            )
            or (
                bool(target_address)
                and str(window.get("address") or "") == target_address
            )
        )
        for window in combine_windows()
    )


def _load_lease_unlocked() -> dict[str, Any] | None:
    current_binding = session_binding()
    active_path = lease_file(current_binding)
    state = _read_lease(active_path)
    if state is None and LEASE_FILE != active_path:
        legacy = _read_lease(LEASE_FILE)
        legacy_binding = legacy.get("session") if legacy is not None else None
        unbound_matches = bool(
            legacy is not None
            and legacy_binding is None
            and legacy_lease_matches_current_session(legacy)
        )
        if legacy is not None and (
            legacy_binding == current_binding or unbound_matches
        ):
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.parent.chmod(0o700)
            if legacy_binding is None:
                legacy["session"] = current_binding
                coordination.atomic_write_json(active_path, legacy)
                LEASE_FILE.unlink(missing_ok=True)
                state = legacy
            else:
                try:
                    LEASE_FILE.replace(active_path)
                except FileNotFoundError:
                    state = _read_lease(active_path)
                else:
                    active_path.chmod(0o600)
                    state = legacy
    if state is None:
        return None
    stored_binding = state.get("session")
    if stored_binding is not None and stored_binding != current_binding:
        raise RuntimeError("coordinate lease belongs to a different display or Hyprland instance")
    return state


def load_lease() -> dict[str, Any] | None:
    if getattr(_LEASE_GUARD_LOCAL, "depth", 0):
        return _load_lease_unlocked()
    with lease_guard():
        return _load_lease_unlocked()


def save_lease(state: dict[str, Any]) -> None:
    coordination.atomic_write_json(lease_file(), state)


@contextmanager
def lease_guard():
    # Serialize migration from the 0.1.x global file, then isolate live state by
    # Wayland display and Hyprland instance so another login cannot wedge this one.
    depth = getattr(_LEASE_GUARD_LOCAL, "depth", 0)
    if depth:
        _LEASE_GUARD_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LEASE_GUARD_LOCAL.depth = depth
        return
    with file_guard(LOCK_FILE):
        with file_guard(lease_lock_file()):
            _LEASE_GUARD_LOCAL.depth = 1
            try:
                yield
            finally:
                _LEASE_GUARD_LOCAL.depth = 0


def wait_for_monitor(name: str, *, present: bool, timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        monitor = next((m for m in hypr_json(["monitors"]) if m.get("name") == name), None)
        if (monitor is not None) == present: return monitor
        time.sleep(0.05)
    raise RuntimeError(f"temporary output {name} did not become {'ready' if present else 'removed'}")


def wait_for_window_fullscreen(address: str, mode: int, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window = next((w for w in combine_windows() if w.get("address") == address), None)
        if window is None:
            raise RuntimeError("leased window closed while changing fullscreen state")
        if int(window.get("fullscreen") or 0) == mode:
            return window
        time.sleep(0.05)
    raise RuntimeError(f"leased window did not reach fullscreen mode {mode}")


def begin_lease(
    arguments: dict[str, Any],
    *,
    selected: dict[str, Any] | None = None,
    owner_thread_id: str | None = None,
    claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true before using globally shared coordinate input")
    if load_lease():
        raise RuntimeError("a coordinate lease is already active; end or recover it first")
    selected = selected or resolve_window(str(arguments["window"]))
    fullscreen_if_needed = arguments.get("fullscreen_if_needed", True)
    if not isinstance(fullscreen_if_needed, bool):
        raise ValueError("fullscreen_if_needed must be a boolean")
    if not selected.get("address"):
        raise RuntimeError("selected window has no Hyprland address")
    ensure_native_input_safe()
    active = hypr_json(["activewindow"])
    active_workspace = hypr_json(["activeworkspace"])
    cursor = hypr_json(["cursorpos"])
    token = secrets.token_urlsafe(18)
    output = f"CODEX-CU-{token[:8]}"
    state = {
        "version": 3,
        "session": session_binding(),
        "token": token,
        "owner_thread_id": owner_thread_id,
        "owner_expires_at": time.time() + coordination.DEFAULT_LEASE_SECONDS,
        "claim_token": claim.get("claim_token") if claim else None,
        "claim_expires_at": claim.get("expires_at") if claim else None,
        "phase": "creating",
        "output": output,
        "target": selected,
        "original": {
            "active_address": active.get("address"),
            "active_workspace": (active_workspace or {}).get("id"),
            "cursor": {"x": cursor.get("x"), "y": cursor.get("y")},
            "target_workspace": selected.get("workspace"),
            "target_monitor": selected.get("monitor"),
            "target_fullscreen": int(selected.get("fullscreen") or 0),
            "target_fullscreen_client": int(selected.get("fullscreen_client") or 0),
        },
        "fallback": {
            "fullscreen_if_needed": fullscreen_if_needed,
            "fullscreen_applied": False,
        },
    }
    save_lease(state)
    try:
        proc = run(["hyprctl", "output", "create", "headless", output])
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "failed to create temporary output")
        monitor = wait_for_monitor(output, present=True)
        workspace = int((monitor.get("activeWorkspace") or {}).get("id"))
        state["phase"] = "output-ready"; state["lease_workspace"] = workspace; save_lease(state)
        address = str(selected["address"])
        hypr_dispatch(f"hl.dsp.window.move({{ workspace = {workspace}, window = 'address:{address}', follow = false }})")
        state["phase"] = "window-moved"; save_lease(state)
        hypr_dispatch(f"hl.dsp.focus({{ window = 'address:{address}' }})")
        state["phase"] = "window-focused"; save_lease(state)
        if fullscreen_if_needed and int(selected.get("fullscreen") or 0) != 2:
            state["fallback"]["fullscreen_applied"] = True
            state["phase"] = "fullscreening"; save_lease(state)
            hypr_dispatch("hl.dsp.window.fullscreen_state({ internal = 2, client = 0 })")
            wait_for_window_fullscreen(address, 2)
        state["phase"] = "active"; save_lease(state)
        current = next((w for w in combine_windows() if w.get("address") == address), None)
        return {
            "lease_token": token,
            "output": output,
            "workspace": workspace,
            "window": bounded_window(current) if current else None,
            "fullscreened_on_fallback_screen": bool(state["fallback"]["fullscreen_applied"]),
            "original_fullscreen": int(selected.get("fullscreen") or 0),
            "interference_boundary": "global keyboard focus and raw pointer are shared until end_coordinate_lease",
        }
    except Exception:
        try: restore_lease(state)
        except Exception: pass
        raise


def require_lease(token: str, owner_thread_id: str | None = None) -> dict[str, Any]:
    state = load_lease()
    if not state: raise RuntimeError("no coordinate lease is active")
    if state.get("token") != token: raise ValueError("lease token does not match the active coordinate lease")
    owner = state.get("owner_thread_id")
    if owner is not None and owner != owner_thread_id:
        raise RuntimeError("coordinate lease belongs to another computer-use agent")
    return state


def require_recovery_access(state: dict[str, Any], owner_thread_id: str | None) -> None:
    lease_owner = state.get("owner_thread_id")
    if lease_owner is None or lease_owner == owner_thread_id:
        return
    owner_live = float(state.get("owner_expires_at") or 0) > time.time()
    claim_token = state.get("claim_token")
    if claim_token:
        owner_live = owner_live or coordination.claim_is_live(
            session_binding(), state.get("target") or {}, lease_owner, str(claim_token)
        )
    if owner_live:
        raise RuntimeError("another computer-use agent still owns the live coordinate lease")


def restore_lease(state: dict[str, Any]) -> dict[str, Any]:
    binding = state.get("session")
    if binding is not None and binding != session_binding():
        raise RuntimeError("refusing to restore a coordinate lease on a different Hyprland instance")
    errors: list[str] = []
    address = str((state.get("target") or {}).get("address") or "")
    original = state.get("original") or {}
    if address and any(w.get("address") == address for w in hypr_windows()):
        try:
            fullscreen = int(original.get("target_fullscreen") or 0)
            fullscreen_client = int(original.get("target_fullscreen_client") or 0)
            hypr_dispatch(f"hl.dsp.focus({{ window = 'address:{address}' }})")
            hypr_dispatch(f"hl.dsp.window.fullscreen_state({{ internal = {fullscreen}, client = {fullscreen_client} }})")
            wait_for_window_fullscreen(address, fullscreen)
        except Exception as exc: errors.append(f"fullscreen restore: {exc}")
        try:
            workspace = int(original["target_workspace"])
            hypr_dispatch(f"hl.dsp.window.move({{ workspace = {workspace}, window = 'address:{address}', follow = false }})")
        except Exception as exc: errors.append(f"window restore: {exc}")
    output = str(state.get("output") or "")
    if output and any(m.get("name") == output for m in hypr_json(["monitors"])):
        proc = run(["hyprctl", "output", "remove", output])
        if proc.returncode: errors.append(proc.stderr.strip() or proc.stdout.strip() or "output removal failed")
        else:
            try: wait_for_monitor(output, present=False)
            except Exception as exc: errors.append(str(exc))
    active_address = str(original.get("active_address") or "")
    if active_address and any(w.get("address") == active_address for w in hypr_windows()):
        try: hypr_dispatch(f"hl.dsp.focus({{ window = 'address:{active_address}' }})")
        except Exception as exc: errors.append(f"focus restore: {exc}")
    else:
        active_workspace = original.get("active_workspace")
        if active_workspace is not None:
            try: hypr_dispatch(f"hl.dsp.focus({{ workspace = {int(active_workspace)} }})")
            except Exception as exc: errors.append(f"workspace restore: {exc}")
    cursor = original.get("cursor") or {}
    if cursor.get("x") is not None and cursor.get("y") is not None:
        try: hypr_dispatch(f"hl.dsp.cursor.move({{ x = {int(cursor['x'])}, y = {int(cursor['y'])} }})")
        except Exception as exc: errors.append(f"pointer restore: {exc}")
    if not errors:
        (lease_file(binding) if binding is not None else LEASE_FILE).unlink(
            missing_ok=True
        )
    return {
        "restored": not errors,
        "errors": [bounded_error(error) for error in errors[:MAX_ERROR_ITEMS]],
        "window_address": address,
        "focus_address": active_address,
        "pointer": cursor,
        "output_removed": output,
        "fullscreen_restored": int(original.get("target_fullscreen") or 0),
    }


def capture_lease(token: str, owner_thread_id: str | None = None) -> dict[str, Any]:
    state = require_lease(token, owner_thread_id)
    fd, name = tempfile.mkstemp(prefix="same-session-coordinate-", suffix=".png")
    os.close(fd); output = Path(name)
    try:
        proc = run(["grim", "-o", str(state["output"]), str(output)], timeout=20)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "coordinate desktop capture failed")
        raw = read_bounded_png(output, "coordinate desktop capture")
    finally:
        output.unlink(missing_ok=True)
    data = base64.b64encode(raw).decode("ascii")
    monitor = next((m for m in hypr_json(["monitors"]) if m.get("name") == state["output"]), None)
    if monitor is None:
        raise RuntimeError("coordinate lease output disappeared before capture metadata was collected")
    scale = float(monitor.get("scale") or 1)
    coordinate_width = int(round(float(monitor["width"]) / scale))
    coordinate_height = int(round(float(monitor["height"]) / scale))
    address = str((state.get("target") or {}).get("address") or "")
    current = next((w for w in combine_windows() if w.get("address") == address), state["target"])
    pixel_size = png_pixel_size(raw)
    metadata = {
        "lease_token": token,
        "output": state["output"],
        "window": bounded_window(current),
        "fullscreened_on_fallback_screen": bool((state.get("fallback") or {}).get("fullscreen_applied")),
        "coordinate_space": {
            "desktop_origin": {"x": int(monitor.get("x") or 0), "y": int(monitor.get("y") or 0)},
            "width": coordinate_width,
            "height": coordinate_height,
            "scale": scale,
            "screenshot_pixels": pixel_size,
            "note": "For global fallback input: desktop_x = origin.x + screenshot_x * width / screenshot_pixels.width, and likewise for y.",
        },
    }
    return {"content": [{"type": "text", "text": json.dumps(metadata, indent=2)}, {"type": "image", "data": data, "mimeType": "image/png"}], "isError": False}


def claim_token_from(arguments: dict[str, Any]) -> str | None:
    token = arguments.get("claim_token")
    if token is None:
        return None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > coordination.MAX_CLAIM_TOKEN_LENGTH
    ):
        raise ValueError(
            f"claim_token must contain 1..{coordination.MAX_CLAIM_TOKEN_LENGTH} characters"
        )
    return token


def _lease_matches_window(state: dict[str, Any], window: dict[str, Any]) -> bool:
    target = state.get("target") or {}
    try:
        return coordination.window_key(target) == coordination.window_key(window)
    except RuntimeError:
        return False


def coordinate_reservation_owner(window: dict[str, Any]) -> str | None:
    state = load_lease()
    if not state or not _lease_matches_window(state, window):
        return None
    return str(state.get("owner_thread_id") or "<legacy-coordinate-lease>")


def prevent_claim_release_during_lease(claim: dict[str, Any]) -> None:
    state = load_lease()
    if state and _lease_matches_window(state, claim.get("window") or {}):
        raise RuntimeError("end the active coordinate lease before releasing its window claim")


def require_global_input_available() -> None:
    if load_lease():
        raise RuntimeError("global input is reserved by an active coordinate lease")


def require_window_access(
    window: dict[str, Any],
    arguments: dict[str, Any],
    owner_thread_id: str | None,
    *,
    mark_inflight: bool = False,
) -> dict[str, Any] | None:
    return coordination.require_window_access(
        session_binding(),
        window,
        owner_thread_id,
        claim_token_from(arguments),
        mark_inflight=mark_inflight,
    )


def require_window_mutation_access(
    window: dict[str, Any],
    arguments: dict[str, Any],
    owner_thread_id: str | None,
    *,
    mark_inflight: bool = False,
) -> dict[str, Any] | None:
    state = load_lease()
    if state and _lease_matches_window(state, window):
        owner = state.get("owner_thread_id")
        if owner is not None and owner != owner_thread_id:
            raise RuntimeError("window is controlled by another agent's coordinate lease")
        raise RuntimeError(
            "window is on an active coordinate fallback; use that lease or end it before targeted input"
        )
    return require_window_access(
        window, arguments, owner_thread_id, mark_inflight=mark_inflight
    )


def finish_claimed_window_access(
    binding: dict[str, Any],
    window: dict[str, Any],
    owner_thread_id: str | None,
    claim: dict[str, Any] | None,
    *,
    renew: bool,
) -> dict[str, Any] | None:
    if claim is None:
        return None
    if owner_thread_id is None:
        raise RuntimeError("claimed operations require host-provided _meta.threadId")
    return coordination.finish_window_access(
        binding,
        window,
        owner_thread_id,
        str(claim["claim_token"]),
        renew=renew,
    )


def rebind_coordinate_claim(
    window: dict[str, Any], owner_thread_id: str, claim: dict[str, Any]
) -> None:
    with lease_guard():
        state = load_lease()
        if not state or not _lease_matches_window(state, window):
            return
        if state.get("owner_thread_id") != owner_thread_id:
            raise RuntimeError("window is reserved by another agent's coordinate lease")
        state["claim_token"] = claim["claim_token"]
        state["claim_expires_at"] = claim["expires_at"]
        state["owner_expires_at"] = time.time() + coordination.DEFAULT_LEASE_SECONDS
        save_lease(state)


def same_coordinate_lease(
    expected: dict[str, Any], current: dict[str, Any]
) -> bool:
    if expected.get("token") != current.get("token"):
        return False
    try:
        return coordination.window_key(
            expected.get("target") or {}
        ) == coordination.window_key(current.get("target") or {})
    except RuntimeError:
        return False


def physical_snapshot() -> dict[str, Any]:
    return {"active_address": hypr_json(["activewindow"]).get("address"), "workspace": hypr_json(["activeworkspace"]).get("id"), "cursor": hypr_json(["cursorpos"])}


def validate_point(window: dict[str, Any], x: float, y: float) -> None:
    size = window.get("size") or []
    if len(size) != 2 or not (0 <= x < float(size[0]) and 0 <= y < float(size[1])):
        raise ValueError(f"coordinate ({x},{y}) is outside window size {size}")


def png_pixel_size(raw: bytes) -> dict[str, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    return {"width": width, "height": height} if width and height else None


def valid_png(raw: bytes) -> bool:
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    offset = 8
    decoder = None
    decoded = 0
    expected_decoded = 0
    row_bytes = 0
    saw_idat = False
    idat_ended = False
    saw_iend = False
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if chunk_end > len(raw):
            return False
        chunk_data = memoryview(raw)[data_start:data_end]
        expected_crc = int.from_bytes(raw[data_end:chunk_end], "big")
        if zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF != expected_crc:
            return False
        if offset == 8:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(chunk_data[:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not width
                or not height
                or width * height > MAX_CAPTURE_PIXELS
                or bit_depth not in valid_depths.get(color_type, set())
                or bytes(chunk_data[10:13]) != b"\x00\x00\x00"
            ):
                return False
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
            row_bytes = (width * channels * bit_depth + 7) // 8 + 1
            expected_decoded = row_bytes * height
            decoder = zlib.decompressobj()
        elif chunk_type == b"IHDR":
            return False
        elif chunk_type == b"IDAT":
            if decoder is None or idat_ended or saw_iend:
                return False
            saw_idat = True
            compressed: bytes | memoryview = chunk_data
            while True:
                remaining = expected_decoded - decoded + 1
                if remaining <= 0:
                    return False
                maximum = min(64 * 1024, remaining)
                try:
                    output = decoder.decompress(compressed, maximum)
                except zlib.error:
                    return False
                first_filter = (-decoded) % row_bytes
                if any(output[index] > 4 for index in range(first_filter, len(output), row_bytes)):
                    return False
                decoded += len(output)
                compressed = decoder.unconsumed_tail
                if compressed:
                    continue
                if len(output) == maximum and not decoder.eof:
                    compressed = b""
                    continue
                break
            if decoder.unused_data:
                return False
        else:
            if saw_idat:
                idat_ended = True
            if chunk_type == b"IEND":
                if length or not saw_idat or decoder is None or not decoder.eof:
                    return False
                saw_iend = True
                offset = chunk_end
                break
        offset = chunk_end
    return saw_iend and offset == len(raw) and decoded == expected_decoded


def read_bounded_png(path: Path, operation: str) -> bytes:
    if path.stat().st_size > MAX_CAPTURE_PNG_BYTES:
        raise RuntimeError(f"{operation} exceeds the {MAX_CAPTURE_PNG_BYTES}-byte MCP transport limit")
    with path.open("rb") as image:
        raw = image.read(MAX_CAPTURE_PNG_BYTES + 1)
    if len(raw) > MAX_CAPTURE_PNG_BYTES:
        raise RuntimeError(f"{operation} exceeds the {MAX_CAPTURE_PNG_BYTES}-byte MCP transport limit")
    if not valid_png(raw):
        raise RuntimeError(f"{operation} returned an invalid PNG")
    return raw


def window_coordinate_space(window: dict[str, Any], raw: bytes) -> dict[str, Any] | None:
    size = window.get("size") or []
    pixels = png_pixel_size(raw)
    if len(size) != 2 or not pixels or not pixels["width"] or not pixels["height"]:
        return None
    width, height = float(size[0]), float(size[1])
    return {
        "window_local": {"width": width, "height": height},
        "screenshot_pixels": pixels,
        "pixel_to_window_scale": {"x": width / pixels["width"], "y": height / pixels["height"]},
        "note": "Pointer tools use window-local coordinates: multiply screenshot x/y by pixel_to_window_scale.",
    }


def resolve_xwindow_id(window: dict[str, Any]) -> str:
    pid = window.get("pid")
    if not pid: raise RuntimeError("XWayland window has no process ID")
    found = run(["xdotool", "search", "--pid", str(pid)])
    ids = [line.strip() for line in found.stdout.splitlines() if line.strip().isdigit()]
    if not ids: raise RuntimeError("xdotool could not resolve the XWayland window")
    title = str(window.get("title") or "")
    exact: list[str] = []
    for xid in ids:
        name = run(["xdotool", "getwindowname", xid])
        if name.returncode == 0 and name.stdout.strip() == title: exact.append(xid)
    if len(exact) == 1: return exact[0]
    if len(ids) == 1: return ids[0]
    raise RuntimeError("XWayland process owns multiple windows; use a unique current title")


def x_pointer_position() -> tuple[int, int]:
    proc = run(["xdotool", "getmouselocation", "--shell"])
    if proc.returncode: raise RuntimeError(proc.stderr.strip() or "failed to snapshot XWayland pointer")
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return int(values["X"]), int(values["Y"])


def xdotool_target(window: dict[str, Any], command: list[str]) -> dict[str, Any]:
    xid = resolve_xwindow_id(window)
    old_x, old_y = x_pointer_position()
    try:
        proc = run(["xdotool", *command], timeout=20)
        if proc.returncode: raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "XWayland targeted input failed")
    finally:
        run(["xdotool", "mousemove", str(old_x), str(old_y)])
    return {"backend": "xwayland-xtest", "xwindow_id": xid}


def _targeted_pointer(
    arguments: dict[str, Any], action: str, *, window: dict[str, Any] | None = None
) -> dict[str, Any]:
    window = window or resolve_window(str(arguments["window"]))
    if window.get("xwayland"):
        ensure_native_input_safe()
    before = physical_snapshot()
    button = str(arguments.get("button") or "left")
    button_number = {"left": "1", "middle": "2", "right": "3"}.get(button)
    if not button_number: raise ValueError("button must be left, right, or middle")

    if action == "click":
        x, y = float(arguments["x"]), float(arguments["y"]); validate_point(window, x, y)
        count = int(arguments.get("count", 1))
        if not 1 <= count <= 3: raise ValueError("count must be between 1 and 3")
        if window.get("xwayland"):
            xid = resolve_xwindow_id(window)
            result = xdotool_target(window, ["mousemove", "--window", xid, str(round(x)), str(round(y)), "click", "--repeat", str(count), "--delay", "40", button_number])
        else:
            ensure_target_pointer_plugin()
            proc = run(["hyprctl", "-j", "cutarget", "click", str(window["address"]), str(x), str(y), button, str(count)])
            if proc.returncode: raise RuntimeError(proc.stderr.strip() or "Wayland targeted click failed")
            result = json.loads(proc.stdout)
    elif action == "scroll":
        x, y = float(arguments["x"]), float(arguments["y"]); validate_point(window, x, y)
        steps = int(arguments["steps"])
        if steps == 0 or abs(steps) > 20: raise ValueError("steps must be between -20 and 20, excluding zero")
        if window.get("xwayland"):
            xid = resolve_xwindow_id(window); wheel = "5" if steps > 0 else "4"
            result = xdotool_target(window, ["mousemove", "--window", xid, str(round(x)), str(round(y)), "click", "--repeat", str(abs(steps)), "--delay", "20", wheel])
        else:
            ensure_target_pointer_plugin()
            proc = run(["hyprctl", "-j", "cutarget", "scroll", str(window["address"]), str(x), str(y), str(steps)])
            if proc.returncode: raise RuntimeError(proc.stderr.strip() or "Wayland targeted scroll failed")
            result = json.loads(proc.stdout)
    else:
        sx, sy, ex, ey = map(float, (arguments["start_x"], arguments["start_y"], arguments["end_x"], arguments["end_y"]))
        validate_point(window, sx, sy); validate_point(window, ex, ey)
        motion_steps = int(arguments.get("motion_steps", 8))
        if not 2 <= motion_steps <= 32: raise ValueError("motion_steps must be between 2 and 32")
        if window.get("xwayland"):
            xid = resolve_xwindow_id(window)
            result = xdotool_target(window, ["mousemove", "--window", xid, str(round(sx)), str(round(sy)), "mousedown", button_number, "mousemove", "--window", xid, str(round(ex)), str(round(ey)), "mouseup", button_number])
        else:
            ensure_target_pointer_plugin()
            proc = run(["hyprctl", "-j", "cutarget", "drag", str(window["address"]), str(sx), str(sy), str(ex), str(ey), button, str(motion_steps)])
            if proc.returncode: raise RuntimeError(proc.stderr.strip() or "Wayland targeted drag failed")
            result = json.loads(proc.stdout)

    if isinstance(result, dict) and result.get("ok") is False: raise RuntimeError(str(result.get("error") or "targeted pointer action failed"))
    after = physical_snapshot()
    unchanged = after == before
    return {"action": action, "window": bounded_window(window), "result": result, "observed_physical_state_unchanged": unchanged, "physical_state_before": before, "physical_state_after": after, "cursor_moved_by_backend": False, "keyboard_focus_changed_by_backend": False, "workspace_changed_by_backend": False}


def targeted_pointer(
    arguments: dict[str, Any], action: str, owner_thread_id: str | None = None
) -> dict[str, Any]:
    window = resolve_window(str(arguments["window"]))
    binding = session_binding()

    def perform() -> dict[str, Any]:
        with coordination.window_guard(binding, window):
            claim = require_window_mutation_access(
                window, arguments, owner_thread_id, mark_inflight=True
            )
            try:
                result = _targeted_pointer(arguments, action, window=window)
            except Exception:
                finish_claimed_window_access(
                    binding, window, owner_thread_id, claim, renew=False
                )
                raise
            finish_claimed_window_access(
                binding, window, owner_thread_id, claim, renew=True
            )
            return result

    if window.get("xwayland"):
        with coordination.global_input_guard():
            require_global_input_available()
            return perform()
    return perform()


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}], "structuredContent": value, "isError": False}


def capture_result(
    arguments: dict[str, Any], *, selected: dict[str, Any] | None = None
) -> dict[str, Any]:
    selected = selected or resolve_window(str(arguments["window"]))
    requested_path = arguments.get("save_path")
    if requested_path:
        output = Path(str(requested_path)).expanduser()
        if not output.is_absolute():
            raise ValueError("save_path must be absolute")
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    else:
        fd, name = tempfile.mkstemp(prefix="same-session-window-", suffix=".png")
    os.close(fd)
    capture = Path(name)
    try:
        proc = run(["grim", "-T", str(selected["capture_id"]), str(capture)], timeout=20)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "exact window capture failed")
        raw = read_bounded_png(capture, "exact window capture")
        data = base64.b64encode(raw).decode("ascii")
        if requested_path:
            capture.replace(output)
    finally:
        capture.unlink(missing_ok=True)
    metadata = {
        "window": bounded_window(selected),
        "coordinate_space": window_coordinate_space(selected, raw),
        "saved_to": str(output) if requested_path else None,
        "focus_changed": False,
        "pointer_moved": False,
        "workspace_changed": False,
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps(metadata, indent=2, ensure_ascii=False)},
            {"type": "image", "data": data, "mimeType": "image/png"},
        ],
        "isError": False,
    }


def status() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    for binary in ("hyprctl", "grim", "xdotool"):
        checks[binary] = shutil.which(binary) is not None
    build_requirements = plugin_build_requirements()
    native_buildable = all(build_requirements.values())
    exact_count = sum(1 for window in combine_windows() if window.get("capture_id")) if checks["hyprctl"] and checks["grim"] else 0
    plugin_loaded = False
    safety_status = None
    if checks["hyprctl"]:
        plugins = run(["hyprctl", "plugin", "list"])
        plugin_loaded = plugins.returncode == 0 and "same-session-target-pointer" in plugins.stdout
        if plugin_loaded:
            probed = run(["hyprctl", "-j", "cutargetstatus"])
            if probed.returncode == 0:
                try:
                    safety_status = json.loads(probed.stdout)
                except json.JSONDecodeError:
                    pass
    native_available = checks["hyprctl"] and (plugin_loaded or native_buildable)
    return {
        "session": "real-current-login",
        "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        "hyprland_instance": bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")),
        "capabilities": {
            "exact_background_window_capture": exact_count > 0,
            "targeted_background_shortcuts": native_available,
            "background_semantic_actions": False,
            "targeted_wayland_pointer": native_available,
            "targeted_xwayland_pointer": native_available and checks["xdotool"],
            "cross_process_window_claims": True,
            "parallel_native_wayland_windows": True,
            "broker_global_input_lane_serialized": True,
            "native_input_currently_safe": bool(safety_status and safety_status.get("safe_to_inject") is True),
            "physical_pointer_seat_is_independent": False,
        },
        "checks": checks,
        "requirements": {
            "native_plugin_build": build_requirements,
            "native_plugin_loaded": plugin_loaded,
            "native_input_safety": safety_status,
            "background_semantic_actions": "requires the separate Computer Use plugin and an enabled AT-SPI session",
        },
        "exact_window_count": exact_count,
        "claim_lease_seconds": {"default": 60, "minimum": 5, "maximum": 300},
        "raw_pointer_note": "Hyprland still has one physical pointer seat. Different native Wayland windows have independent broker lanes; this broker serializes its XWayland and fallback input, but separate same-user processes do not share that lock. The physical cursor, keyboard focus, and workspace are preserved by normal targeted actions.",
    }


def send_window_shortcut(
    arguments: dict[str, Any], owner_thread_id: str | None = None
) -> dict[str, Any]:
    address = str(arguments["address"])
    window = next((window for window in combine_windows() if window.get("address") == address), None)
    if not address.startswith("0x") or window is None:
        raise ValueError("address must be a live Hyprland window address from list_session_windows")
    key = str(arguments["key"])
    modifiers = str(arguments.get("modifiers") or "")
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+", key):
        raise ValueError(
            "key must be a Hyprland key name containing only letters, digits, underscore, plus, or hyphen"
        )
    if not re.fullmatch(r"[A-Za-z ]*", modifiers):
        raise ValueError("modifiers may contain only modifier names and spaces")
    binding = session_binding()

    def perform() -> dict[str, Any]:
        with coordination.window_guard(binding, window):
            claim = require_window_mutation_access(
                window, arguments, owner_thread_id, mark_inflight=True
            )
            try:
                ensure_native_input_safe()
                proc = run(
                    [
                        "hyprctl",
                        "dispatch",
                        f"hl.dsp.send_shortcut({{ mods = '{modifiers}', key = '{key}', window = 'address:{address}' }})",
                    ]
                )
                if proc.returncode or "ok" not in proc.stdout.lower():
                    raise RuntimeError(
                        proc.stderr.strip()
                        or proc.stdout.strip()
                        or "targeted shortcut failed"
                    )
                result = {
                    "sent": True,
                    "address": address,
                    "key": key,
                    "modifiers": modifiers,
                    "focus_changed": False,
                    "pointer_moved": False,
                }
            except Exception:
                finish_claimed_window_access(
                    binding, window, owner_thread_id, claim, renew=False
                )
                raise
            finish_claimed_window_access(
                binding, window, owner_thread_id, claim, renew=True
            )
            return result

    if window.get("xwayland"):
        with coordination.global_input_guard():
            require_global_input_available()
            return perform()
    return perform()


def require_owner(owner_thread_id: str | None, tool_name: str) -> str:
    if owner_thread_id is None:
        raise RuntimeError(f"{tool_name} requires host-provided _meta.threadId")
    return owner_thread_id


def call_tool(
    name: str, arguments: dict[str, Any], owner_thread_id: str | None = None
) -> dict[str, Any]:
    if name == "session_status": return text_result(status())
    if name == "list_session_windows":
        limit = arguments.get("limit")
        if limit is None:
            limit = MAX_WINDOWS_PER_PAGE
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_WINDOWS_PER_PAGE:
            raise ValueError(f"limit must be an integer between 1 and {MAX_WINDOWS_PER_PAGE}")
        cursor = arguments.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and len(cursor) <= 20 and cursor.isascii() and cursor.isdigit():
            offset = int(cursor)
        else:
            raise ValueError("cursor must be the next_cursor string from a previous result")
        windows = [bounded_window(window) for window in combine_windows()]
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
    if name == "claim_session_window":
        owner = require_owner(owner_thread_id, name)
        lease_seconds = arguments.get("lease_seconds", coordination.DEFAULT_LEASE_SECONDS)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("lease_seconds must be an integer")
        window = resolve_window(str(arguments["window"]))
        claim = coordination.claim_window(
            session_binding(),
            window,
            owner,
            lease_seconds,
            reservation_owner=lambda: coordinate_reservation_owner(window),
            after_claim=lambda claim: rebind_coordinate_claim(window, owner, claim),
        )
        return text_result(claim)
    if name == "release_session_window":
        owner = require_owner(owner_thread_id, name)
        token = claim_token_from(arguments)
        if token is None:
            raise ValueError("claim_token is required")
        return text_result(
            coordination.release_claim(
                session_binding(),
                token,
                owner,
                before_release=prevent_claim_release_during_lease,
            )
        )
    if name == "list_window_claims":
        limit = arguments.get("limit")
        if limit is None:
            limit = MAX_CLAIMS_PER_PAGE
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_CLAIMS_PER_PAGE
        ):
            raise ValueError(
                f"limit must be an integer between 1 and {MAX_CLAIMS_PER_PAGE}"
            )
        cursor = arguments.get("cursor")
        if cursor is None:
            offset = 0
        elif (
            isinstance(cursor, str)
            and len(cursor) <= 20
            and cursor.isascii()
            and cursor.isdigit()
        ):
            offset = int(cursor)
        else:
            raise ValueError("cursor must be the next_cursor string from a previous result")
        claims = coordination.list_claims(session_binding())
        page: list[dict[str, Any]] = []
        end = offset
        while end < len(claims) and len(page) < limit:
            candidate = [*page, claims[end]]
            candidate_end = end + 1
            candidate_result = {
                "claims": candidate,
                "next_cursor": str(candidate_end)
                if candidate_end < len(claims)
                else None,
            }
            encoded = json.dumps(
                candidate_result, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if len(encoded) > MAX_CLAIM_RESULT_BYTES:
                break
            page = candidate
            end = candidate_end
        if not page and offset < len(claims):
            raise RuntimeError("a window claim exceeds the bounded listing result size")
        return text_result(
            {
                "claims": page,
                "next_cursor": str(end) if end < len(claims) else None,
            }
        )
    if name == "capture_session_window":
        window = resolve_window(str(arguments["window"]))
        binding = session_binding()
        with coordination.window_guard(binding, window):
            claim = require_window_access(
                window, arguments, owner_thread_id, mark_inflight=True
            )
            try:
                result = capture_result(arguments, selected=window)
            except Exception:
                finish_claimed_window_access(
                    binding, window, owner_thread_id, claim, renew=False
                )
                raise
            finish_claimed_window_access(
                binding, window, owner_thread_id, claim, renew=True
            )
            return result
    if name == "send_window_shortcut":
        return text_result(send_window_shortcut(arguments, owner_thread_id))
    if name == "targeted_pointer_click":
        return text_result(targeted_pointer(arguments, "click", owner_thread_id))
    if name == "targeted_pointer_scroll":
        return text_result(targeted_pointer(arguments, "scroll", owner_thread_id))
    if name == "targeted_pointer_drag":
        return text_result(targeted_pointer(arguments, "drag", owner_thread_id))
    if name == "begin_coordinate_lease":
        window = resolve_window(str(arguments["window"]))
        binding = session_binding()
        with coordination.global_input_guard():
            with coordination.window_guard(binding, window):
                with lease_guard():
                    require_global_input_available()
                    claim = require_window_access(
                        window, arguments, owner_thread_id, mark_inflight=True
                    )
                    try:
                        result = begin_lease(
                            arguments,
                            selected=window,
                            owner_thread_id=owner_thread_id,
                            claim=claim,
                        )
                    except Exception:
                        finish_claimed_window_access(
                            binding, window, owner_thread_id, claim, renew=False
                        )
                        raise
                    renewed = finish_claimed_window_access(
                        binding, window, owner_thread_id, claim, renew=True
                    )
                    state = load_lease()
                    if state:
                        state["owner_expires_at"] = (
                            time.time() + coordination.DEFAULT_LEASE_SECONDS
                        )
                        if renewed is not None:
                            state["claim_token"] = renewed["claim_token"]
                            state["claim_expires_at"] = renewed["expires_at"]
                        save_lease(state)
                    return text_result(result)
    if name == "capture_coordinate_desktop":
        state = require_lease(str(arguments["lease_token"]), owner_thread_id)
        window = state.get("target") or {}
        with coordination.window_guard(session_binding(), window):
            with lease_guard():
                state = require_lease(str(arguments["lease_token"]), owner_thread_id)
                lease_owner = state.get("owner_thread_id")
                claim = None
                if isinstance(lease_owner, str):
                    claim = coordination.renew_owned_claim(
                        session_binding(), window, lease_owner, mark_inflight=True
                    )
                    if claim:
                        state["claim_token"] = claim["claim_token"]
                        state["claim_expires_at"] = claim["expires_at"]
                state["owner_expires_at"] = time.time() + coordination.DEFAULT_LEASE_SECONDS
                save_lease(state)
                try:
                    result = capture_lease(
                        str(arguments["lease_token"]), owner_thread_id
                    )
                except Exception:
                    finish_claimed_window_access(
                        session_binding(),
                        window,
                        owner_thread_id,
                        claim,
                        renew=False,
                    )
                    raise
                renewed = finish_claimed_window_access(
                    session_binding(), window, owner_thread_id, claim, renew=True
                )
                state = require_lease(
                    str(arguments["lease_token"]), owner_thread_id
                )
                state["owner_expires_at"] = (
                    time.time() + coordination.DEFAULT_LEASE_SECONDS
                )
                if renewed is not None:
                    state["claim_token"] = renewed["claim_token"]
                    state["claim_expires_at"] = renewed["expires_at"]
                save_lease(state)
                return result
    if name == "end_coordinate_lease":
        with coordination.global_input_guard():
            with lease_guard():
                expected = require_lease(
                    str(arguments["lease_token"]), owner_thread_id
                )
                window = expected.get("target") or {}
            with coordination.window_guard(session_binding(), window):
                with lease_guard():
                    state = require_lease(
                        str(arguments["lease_token"]), owner_thread_id
                    )
                    if not same_coordinate_lease(expected, state):
                        raise RuntimeError(
                            "coordinate lease changed while waiting for its window; retry"
                        )
                    return text_result(restore_lease(state))
    if name == "recover_coordinate_lease":
        with coordination.global_input_guard():
            with lease_guard():
                expected = load_lease()
                if not expected:
                    return text_result(
                        {"restored": True, "message": "no unfinished coordinate lease"}
                    )
                require_recovery_access(expected, owner_thread_id)
                window = expected.get("target") or {}
            with coordination.window_guard(session_binding(), window):
                with lease_guard():
                    state = load_lease()
                    if not state:
                        return text_result(
                            {"restored": True, "message": "no unfinished coordinate lease"}
                        )
                    if not same_coordinate_lease(expected, state):
                        raise RuntimeError(
                            "coordinate lease changed while waiting for its window; retry"
                        )
                    require_recovery_access(state, owner_thread_id)
                    return text_result(restore_lease(state))
    raise ValueError(f"unknown tool: {name}")


def owner_from_params(params: dict[str, Any]) -> str | None:
    metadata = params.get("_meta")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("tools/call params._meta must be an object")
    owner = metadata.get("threadId")
    if owner is None:
        return None
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or len(owner) > coordination.MAX_OWNER_LENGTH
    ):
        raise ValueError("tools/call params._meta.threadId must be a non-empty string")
    return owner


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None: return None
    method = message.get("method")
    try:
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            result = {"protocolVersion": negotiated, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO, "instructions": "Operate the user's real logged-in Hyprland session. Claim each target window before capture or mutation, renew longer work, and release it during cleanup. Different native Wayland windows can progress concurrently; semantic AT-SPI actions require the separate Computer Use plugin."}
        elif method == "tools/list": result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict): raise ValueError("tool params must be an object")
            args = params.get("arguments") or {}
            if not isinstance(args, dict): raise ValueError("tool arguments must be an object")
            result = call_tool(
                str(params.get("name") or ""), args, owner_from_params(params)
            )
        elif method == "ping": result = {}
        else: return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": bounded_error(f"method not found: {method}")}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": bounded_error(exc)}}


def main() -> int:
    lock = threading.Lock()
    workers: list[threading.Thread] = []
    def process(message: dict[str, Any]) -> None:
        response = dispatch(message)
        if response is not None:
            with lock:
                print(json.dumps(response, separators=(",", ":")), flush=True)
    for line in sys.stdin:
        try: message = json.loads(line)
        except Exception as exc:
            with lock: print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": bounded_error(exc)}}), flush=True)
            continue
        worker = threading.Thread(target=process, args=(message,), daemon=True)
        workers.append(worker); worker.start()
    for worker in workers: worker.join(timeout=30)
    return 0
