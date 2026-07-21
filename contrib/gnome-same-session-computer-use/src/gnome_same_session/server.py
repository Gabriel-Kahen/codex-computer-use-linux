import base64
import json
import math
import os
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

from .claims import DEFAULT_LEASE_SECONDS
from .claims import MAX_LEASE_SECONDS
from .claims import MAX_OWNER_CHARS
from .claims import MIN_LEASE_SECONDS
from .claims import ClaimRegistry
from .claims import atomic_write_json
from .claims import broker_is_alive
from .claims import current_broker_identity
from .claims import current_session_identity
from .claims import file_guard

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    from gi.repository import GLib
except (ImportError, ValueError):
    Gio = None
    GLib = None


SERVER_INFO = {"name": "gnome-same-session-computer-use", "version": "0.2.0"}
PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_STDOUT_LINE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_PNG_BYTES = 5 * 1024 * 1024
MAX_CAPTURE_PIXELS = 7680 * 4320
# MCP tool results repeat structured content in a text block. Keep the source page
# small enough that the combined result and wrapper remain below Codex's 12 KiB cap.
MAX_WINDOW_RESULT_BYTES = 4 * 1024
MAX_WINDOWS_PER_PAGE = 20
MAX_CLAIM_RESULT_BYTES = 2 * 1024
MAX_CLAIMS_PER_PAGE = 20
MAX_WINDOW_TEXT_CHARS = 512
MAX_ERROR_TEXT_CHARS = 2048
MAX_RESPONSE_COLLECTION_ITEMS = 8
MAX_RESPONSE_DEPTH = 4
CLAIMED_LEASE_PROTOCOL_VERSION = 2
CLAIMED_LEASE_CAPABILITY = "claimed_focus_leases"
BUS_NAME = "org.gnome.Shell.Extensions.BackgroundComputerUse"
OBJECT_PATH = "/org/gnome/Shell/Extensions/BackgroundComputerUse"
INTERFACE = BUS_NAME
STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / SERVER_INFO["name"]
SESSION_IDENTITY = current_session_identity()
STATE_DIR = STATE_ROOT / "sessions" / SESSION_IDENTITY
LEASE_FILE = STATE_DIR / "focus-lease.json"
LOCK_FILE = STATE_DIR / "focus-lease.lock"
INPUT_FILE = STATE_DIR / "input.lock"
LEGACY_LEASE_FILE = STATE_ROOT / "focus-lease.json"
LEGACY_LOCK_FILE = STATE_ROOT / "focus-lease.lock"
MIGRATION_LOCK_FILE = STATE_ROOT / "session-migration.lock"
BROKER_IDENTITY = current_broker_identity()
CLAIMS = ClaimRegistry(STATE_DIR, SESSION_IDENTITY, broker=BROKER_IDENTITY)
INPUT_LOCK = threading.Lock()
DBUS_LOCK = threading.Lock()
_DBUS_CONNECTION = None


def tool(name: str, description: str, properties: dict[str, Any], required: list[str], *, read_only: bool = False, idempotent: bool = False, open_world: bool | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": idempotent,
            "openWorldHint": not read_only if open_world is None else open_world,
        },
    }


WINDOW = {"type": "string", "minLength": 1, "maxLength": 512, "description": "Stable GNOME window id, exact app id/class, or title substring."}
TOKEN = {"type": "string", "minLength": 64, "maxLength": 256, "description": "Token returned by begin_focus_lease."}
CLAIM_TOKEN = {"type": "string", "minLength": 64, "maxLength": 256, "description": "Opaque token returned by claim_session_window."}
CURSOR = {"type": ["string", "null"], "maxLength": 20}
POINT = {"type": "number", "minimum": 0}
TOOLS = [
    tool("session_status", "Report GNOME/Mutter integration health and exact capability boundaries.", {}, [], read_only=True, idempotent=True),
    tool("list_session_windows", "List one bounded page of windows in the user's real GNOME Shell session.", {"cursor": CURSOR, "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_WINDOWS_PER_PAGE}}, [], read_only=True, idempotent=True),
    tool("claim_session_window", "Exclusively claim one window for this Codex thread while allowing other threads to claim different windows concurrently.", {"window": WINDOW, "lease_seconds": {"type": "integer", "minimum": MIN_LEASE_SECONDS, "maximum": MAX_LEASE_SECONDS, "default": DEFAULT_LEASE_SECONDS}}, ["window"]),
    tool("release_session_window", "Release a window claim owned by this Codex thread.", {"claim_token": CLAIM_TOKEN}, ["claim_token"], idempotent=True),
    tool("list_window_claims", "List one bounded page of live window claims without exposing their capability tokens.", {"cursor": CURSOR, "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_CLAIMS_PER_PAGE}}, [], read_only=True, idempotent=True),
    tool("capture_session_window", "Deprecated compatibility tool. Capture an exact focused window and optionally write save_path. Prefer get_session_window_capture for inline capture or save_session_window_capture for writes.", {"window": WINDOW, "save_path": {"type": ["string", "null"]}, "claim_token": CLAIM_TOKEN}, ["window"], idempotent=True),
    tool("get_session_window_capture", "Return an inline PNG of an exact focused window without creating a caller-selected file. An unfocused window must first be placed under an acknowledged focus lease.", {"window": WINDOW, "claim_token": CLAIM_TOKEN}, ["window"], read_only=True, idempotent=True),
    tool("save_session_window_capture", "Capture an exact focused window and atomically create or replace an absolute PNG path. Also returns the PNG inline.", {"window": WINDOW, "save_path": {"type": "string", "minLength": 1, "maxLength": 4096}, "claim_token": CLAIM_TOKEN}, ["window", "save_path"], idempotent=True, open_world=False),
    tool("begin_focus_lease", "Journal desktop state, switch to and focus an existing window, and authorize brief global-seat contention until restored.", {"window": WINDOW, "acknowledge_interference": {"type": "boolean"}, "claim_token": CLAIM_TOKEN}, ["window", "acknowledge_interference"]),
    tool("lease_pointer_click", "Click a leased window using Mutter's global virtual seat, restoring the pointer immediately afterward.", {"lease_token": TOKEN, "claim_token": CLAIM_TOKEN, "x": POINT, "y": POINT, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}, "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1}}, ["lease_token", "x", "y"]),
    tool("lease_pointer_scroll", "Scroll in a leased window using Mutter's global virtual seat, restoring the pointer immediately afterward.", {"lease_token": TOKEN, "claim_token": CLAIM_TOKEN, "x": POINT, "y": POINT, "steps": {"type": "integer", "minimum": -20, "maximum": 20}}, ["lease_token", "x", "y", "steps"]),
    tool("lease_pointer_drag", "Drag in a leased window using Mutter's global virtual seat, restoring the pointer after release.", {"lease_token": TOKEN, "claim_token": CLAIM_TOKEN, "start_x": POINT, "start_y": POINT, "end_x": POINT, "end_y": POINT, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}, "motion_steps": {"type": "integer", "minimum": 2, "maximum": 32, "default": 8}}, ["lease_token", "start_x", "start_y", "end_x", "end_y"]),
    tool("send_lease_shortcut", "Send one key or shortcut to the currently focused leased window through Mutter's global virtual keyboard.", {"lease_token": TOKEN, "claim_token": CLAIM_TOKEN, "key": {"type": "string", "minLength": 1, "maxLength": 64}, "modifiers": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": ["CTRL", "SHIFT", "ALT", "SUPER"]}, "default": []}}, ["lease_token", "key"]),
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


def bounded_text(value: Any, limit: int = MAX_WINDOW_TEXT_CHARS) -> str:
    return str("" if value is None else value)[:limit]


def bounded_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if abs(value) <= 2**53 - 1 else None


def bounded_json_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return bounded_text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bounded_number(value)
    if depth >= MAX_RESPONSE_DEPTH:
        return None
    if isinstance(value, list):
        return [bounded_json_value(item, depth + 1) for item in value[:MAX_RESPONSE_COLLECTION_ITEMS]]
    if isinstance(value, dict):
        return {
            bounded_text(key, 128): bounded_json_value(item, depth + 1)
            for key, item in list(value.items())[:MAX_RESPONSE_COLLECTION_ITEMS]
        }
    return bounded_text(value)


def window_summary(window: Any) -> dict[str, Any]:
    if not isinstance(window, dict):
        window = {}
    frame = window.get("frame")
    if not isinstance(frame, dict):
        frame = {}
    return {
        "id": bounded_text(window.get("id")),
        "title": bounded_text(window.get("title")),
        "wm_class": bounded_text(window.get("wm_class")),
        "app_id": bounded_text(window.get("app_id")),
        "pid": bounded_number(window.get("pid")),
        "workspace": bounded_number(window.get("workspace")),
        "monitor": bounded_number(window.get("monitor")),
        "focused": window.get("focused") is True,
        "minimized": window.get("minimized") is True,
        "fullscreen": window.get("fullscreen") is True,
        "client_type": bounded_text(window.get("client_type")),
        "frame": {
            key: bounded_number(frame.get(key))
            for key in ("x", "y", "width", "height")
        },
    }


def lease_window_id(state: dict[str, Any] | None) -> str:
    target = (state or {}).get("target")
    return str(target.get("id") if isinstance(target, dict) else target or "")


def windows() -> list[dict[str, Any]]:
    value = dbus_call("ListWindows")
    if not isinstance(value, list) or any(not isinstance(window, dict) for window in value):
        raise RuntimeError("GNOME integration returned an invalid window list")
    return value


def resolve_window(query: Any) -> dict[str, Any]:
    if not isinstance(query, str) or not query or len(query) > 512:
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
        summaries = [window_summary(window) for window in matches[:MAX_RESPONSE_COLLECTION_ITEMS]]
        choices = ", ".join(
            f"{window['app_id']} {window['title']} ({window['id']})"
            for window in summaries
        )
        message = f"window query is ambiguous; use a stable id: {choices}"
        raise RuntimeError(message[:MAX_ERROR_TEXT_CHARS])
    return matches[0]


def migrate_legacy_lease() -> None:
    if LEASE_FILE.exists() or not LEGACY_LEASE_FILE.exists():
        return
    with file_guard(MIGRATION_LOCK_FILE):
        if LEASE_FILE.exists() or not LEGACY_LEASE_FILE.exists():
            return
        with file_guard(LEGACY_LOCK_FILE):
            try:
                state = json.loads(LEGACY_LEASE_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                return
            if not isinstance(state, dict):
                return
            journal_identity = state.get("session_identity")
            if journal_identity is not None:
                matches = journal_identity == SESSION_IDENTITY
            elif state.get("version") == 2 and isinstance(state.get("shell_instance"), str):
                try:
                    current = dbus_call("Status")
                except Exception:
                    return
                matches = (
                    isinstance(current, dict)
                    and current.get("shell_instance") == state["shell_instance"]
                )
            else:
                matches = False
            if not matches:
                return
            state["session_identity"] = SESSION_IDENTITY
            atomic_write_json(LEASE_FILE, state)
            LEGACY_LEASE_FILE.unlink(missing_ok=True)


def load_lease() -> dict[str, Any] | None:
    migrate_legacy_lease()
    if not LEASE_FILE.exists():
        return None
    try:
        state = json.loads(LEASE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read the private focus-lease journal: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("private focus-lease journal has an unsupported format")
    journal_identity = state.get("session_identity")
    if journal_identity is not None and journal_identity != SESSION_IDENTITY:
        raise RuntimeError("focus-lease journal belongs to another GNOME session")
    return state


def save_lease(state: dict[str, Any]) -> None:
    state["session_identity"] = SESSION_IDENTITY
    atomic_write_json(LEASE_FILE, state)


def shell_status() -> dict[str, Any]:
    value = dbus_call("Status")
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("shell_instance"), str)
        or not 1 <= len(value["shell_instance"]) <= 256
    ):
        raise RuntimeError("GNOME integration returned an invalid Shell session identity")
    return value


def shell_supports_claimed_leases(integration: dict[str, Any]) -> bool:
    capabilities = integration.get("capabilities")
    return (
        type(integration.get("protocol_version")) is int
        and integration["protocol_version"] >= CLAIMED_LEASE_PROTOCOL_VERSION
        and isinstance(capabilities, list)
        and CLAIMED_LEASE_CAPABILITY in capabilities
    )


def require_claimed_lease_support(integration: dict[str, Any]) -> None:
    if not shell_supports_claimed_leases(integration):
        raise RuntimeError(
            "the installed GNOME Shell extension does not support parallel window claims; "
            "run install-gnome-integration and reload the GNOME session"
        )


def require_shell_instance(expected_shell_instance: str) -> dict[str, Any]:
    integration = shell_status()
    if integration["shell_instance"] != expected_shell_instance:
        raise RuntimeError(
            "GNOME Shell restarted while resolving or acting on the window; resolve it again"
        )
    return integration


def resolve_window_for_shell(
    query: Any,
    expected_shell_instance: str | None = None,
) -> tuple[dict[str, Any], str]:
    current_shell_instance = shell_status()["shell_instance"]
    if expected_shell_instance is None:
        expected_shell_instance = current_shell_instance
    elif current_shell_instance != expected_shell_instance:
        raise RuntimeError(
            "GNOME Shell restarted while resolving or acting on the window; resolve it again"
        )
    selected = resolve_window(query)
    require_shell_instance(expected_shell_instance)
    return selected, expected_shell_instance


def owner_from_params(params: dict[str, Any]) -> str | None:
    metadata = params.get("_meta")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("tools/call params._meta must be an object")
    owner = metadata.get("threadId")
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner or len(owner) > MAX_OWNER_CHARS or "\0" in owner:
        raise ValueError(
            f"params._meta.threadId must contain between 1 and {MAX_OWNER_CHARS} characters"
        )
    return owner


def shell_recovery_seconds(expires_at: float) -> str:
    remaining = max(0.001, min(float(MAX_LEASE_SECONDS), expires_at - time.time()))
    return f"{remaining:.6f}"


def claim_recovery_seconds(claim: dict[str, Any]) -> str:
    deadline = max(
        float(claim.get("expires_at") or 0),
        float(claim.get("inflight_until") or 0),
    )
    return shell_recovery_seconds(deadline)


def begin_lease(
    arguments: dict[str, Any],
    owner: str | None = None,
    selected: dict[str, Any] | None = None,
    claim: dict[str, Any] | None = None,
    *,
    expected_shell_instance: str | None = None,
) -> dict[str, Any]:
    if arguments.get("acknowledge_interference") is not True:
        raise ValueError("acknowledge_interference must be true because GNOME uses one global input seat")
    if load_lease():
        raise RuntimeError("a focus lease is already active; end or recover it first")
    selected = selected or resolve_window(arguments.get("window"))
    if expected_shell_instance is not None:
        require_shell_instance(expected_shell_instance)
    if claim:
        prepared = dbus_call(
            "BeginClaimedLease", str(selected["id"]), claim_recovery_seconds(claim)
        )
    else:
        prepared = dbus_call("BeginLease", str(selected["id"]))
    if not isinstance(prepared, dict):
        raise RuntimeError("GNOME integration returned an invalid lease preparation result")
    capability = prepared.get("capability")
    if not isinstance(capability, str) or not 64 <= len(capability) <= 256:
        raise RuntimeError("GNOME integration returned an invalid lease capability")
    state = {
        "version": 3,
        "token": capability,
        "phase": "prepared",
        "target": prepared.get("target") or selected,
        "original": prepared.get("original"),
        "shell_instance": prepared.get("shell_instance"),
        "owner_thread_id": owner,
        "broker": BROKER_IDENTITY,
        "claim_token": claim.get("claim_token") if claim else None,
    }
    try:
        save_lease(state)
        if (
            expected_shell_instance is not None
            and state["shell_instance"] != expected_shell_instance
        ):
            raise RuntimeError(
                "GNOME Shell restarted while preparing the focus lease; resolve the window again"
            )
        if str((prepared.get("target") or {}).get("id")) != str(selected["id"]):
            raise RuntimeError(
                "GNOME Shell prepared a different focus-lease target; resolve the window again"
            )
        if expected_shell_instance is not None:
            require_shell_instance(expected_shell_instance)
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
        "window": window_summary(selected),
        "interference_boundary": "workspace and keyboard focus remain leased; pointer is briefly moved and restored per pointer action",
        "capture": "capture_session_window is exact while this lease remains active",
    }


def require_lease(token: str, owner: str | None = None) -> dict[str, Any]:
    if not isinstance(token, str) or not 64 <= len(token) <= 256:
        raise ValueError("lease_token has an invalid length")
    state = load_lease()
    if not state:
        raise RuntimeError("no focus lease is active")
    if not secrets.compare_digest(str(state.get("token") or ""), token):
        raise ValueError("lease token does not match the active focus lease")
    lease_owner = state.get("owner_thread_id")
    if lease_owner is not None and owner != lease_owner:
        raise RuntimeError("focus lease belongs to another computer-use agent")
    return state


def require_bound_claim(state: dict[str, Any], claim: dict[str, Any] | None) -> None:
    bound_token = state.get("claim_token")
    if bound_token is None:
        return
    if claim is None or not secrets.compare_digest(str(claim.get("claim_token") or ""), bound_token):
        raise RuntimeError("the focus lease's window claim expired or was replaced; recover the focus lease")


def restore_lease(state: dict[str, Any], *, recovery: bool = False) -> dict[str, Any]:
    try:
        result = dbus_call("RecoverLease" if recovery else "RestoreLease", str(state["token"]))
    except Exception as exc:
        result = {"restored": False, "errors": [str(exc)]}
        if recovery and state.get("phase") in {"prepared", "active"} and state.get("shell_instance"):
            try:
                shell = dbus_call("Status")
            except Exception:
                shell = None
            if (
                isinstance(shell, dict)
                and shell.get("shell_instance") == state["shell_instance"]
                and shell.get("lease_phase") is None
            ):
                prepared = state["phase"] == "prepared"
                result = {
                    "restored": prepared,
                    "recovery_complete": True,
                    "errors": [],
                    "state": shell,
                    "expired_pending_lease": prepared,
                    "recovery_outcome_unknown": not prepared,
                }
    if not isinstance(result, dict):
        result = {"restored": False, "errors": ["GNOME integration returned an invalid restoration result"]}
    raw_errors = result.get("errors")
    errors = (
        [bounded_text(error, MAX_ERROR_TEXT_CHARS) for error in raw_errors[:MAX_RESPONSE_COLLECTION_ITEMS]]
        if isinstance(raw_errors, list)
        else ["GNOME integration returned an invalid restoration errors field"]
    )
    raw_missing_windows = result.get("missing_windows", [])
    if isinstance(raw_missing_windows, list):
        missing_windows = [
            bounded_text(window)
            for window in raw_missing_windows[:MAX_RESPONSE_COLLECTION_ITEMS]
        ]
    else:
        missing_windows = []
        errors.append("GNOME integration returned an invalid missing_windows field")
    recovery_complete = result.get("recovery_complete", result.get("restored")) is True and not errors
    restored = result.get("restored") is True and recovery_complete and not missing_windows
    if recovery_complete:
        LEASE_FILE.unlink(missing_ok=True)
    target = state.get("target")
    target_id = target.get("id") if isinstance(target, dict) else target
    return {
        "restored": restored,
        "recovery_complete": recovery_complete,
        "errors": errors,
        "missing_windows": missing_windows,
        "post_restore_state": bounded_json_value(result.get("state")),
        "expired_pending_lease": result.get("expired_pending_lease") is True,
        "recovery_outcome_unknown": result.get("recovery_outcome_unknown") is True,
        "target": bounded_text(target_id),
        "journal_retained": not recovery_complete,
    }


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


def read_bounded_png(path: Path) -> bytes:
    if path.stat().st_size > MAX_CAPTURE_PNG_BYTES:
        raise RuntimeError(f"captured PNG exceeds the {MAX_CAPTURE_PNG_BYTES}-byte MCP transport limit")
    with path.open("rb") as image:
        raw = image.read(MAX_CAPTURE_PNG_BYTES + 1)
    if len(raw) > MAX_CAPTURE_PNG_BYTES:
        raise RuntimeError(f"captured PNG exceeds the {MAX_CAPTURE_PNG_BYTES}-byte MCP transport limit")
    if not valid_png(raw):
        raise RuntimeError("focused-window capture returned an invalid PNG")
    return raw


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


def capture_window(
    arguments: dict[str, Any],
    owner: str | None = None,
    selected: dict[str, Any] | None = None,
    claim: dict[str, Any] | None = None,
    *,
    expected_shell_instance: str | None = None,
) -> dict[str, Any]:
    selected = selected or resolve_window(arguments.get("window"))
    active_lease = load_lease()
    if active_lease and active_lease.get("owner_thread_id") is not None:
        if active_lease["owner_thread_id"] != owner:
            raise RuntimeError("capture cannot use another computer-use agent's focus lease")
        require_bound_claim(active_lease, claim)
    permitted = selected.get("focused") or (
        active_lease and active_lease.get("phase") == "active" and
        lease_window_id(active_lease) == str(selected.get("id"))
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
        if expected_shell_instance is not None:
            require_shell_instance(expected_shell_instance)
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
        if expected_shell_instance is None:
            current = resolve_window(str(selected["id"]))
        else:
            current, _ = resolve_window_for_shell(
                str(selected["id"]), expected_shell_instance
            )
        if not current.get("focused"):
            raise RuntimeError("the leased window lost focus during capture; screenshot discarded")
        raw = read_bounded_png(temporary)
        temporary.chmod(0o600)
        if destination:
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    summary = window_summary(selected)
    frame = summary["frame"]
    metadata = {
        "window": summary,
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


def pointer_action(
    arguments: dict[str, Any],
    action: str,
    owner: str | None = None,
    claim: dict[str, Any] | None = None,
    expected_target: str | None = None,
) -> dict[str, Any]:
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
        state = require_lease(arguments.get("lease_token"), owner)
        if expected_target is not None and lease_window_id(state) != expected_target:
            raise RuntimeError("focus lease changed while the pointer action was waiting")
        require_bound_claim(state, claim)
        with INPUT_LOCK, file_guard(INPUT_FILE):
            result = dbus_call("InjectPointer", state["token"], json.dumps(request, separators=(",", ":")))
    return {
        "lease_token": bounded_text(state["token"], 256),
        "window": window_summary(state["target"]),
        "transaction": bounded_json_value(result),
        "global_seat_used": True,
    }


def send_shortcut(
    arguments: dict[str, Any],
    owner: str | None = None,
    claim: dict[str, Any] | None = None,
    expected_target: str | None = None,
) -> dict[str, Any]:
    modifiers = arguments.get("modifiers", [])
    allowed = {"CTRL", "SHIFT", "ALT", "SUPER"}
    if not isinstance(modifiers, list) or len(modifiers) > 4 or any(type(value) is not str or value not in allowed for value in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError("modifiers must be a unique array containing at most CTRL, SHIFT, ALT, and SUPER")
    key = arguments.get("key")
    if not isinstance(key, str) or not key or len(key) > 64:
        raise ValueError("key must contain between 1 and 64 characters")
    request = {"key": key, "modifiers": modifiers}
    with file_guard(LOCK_FILE):
        state = require_lease(arguments.get("lease_token"), owner)
        if expected_target is not None and lease_window_id(state) != expected_target:
            raise RuntimeError("focus lease changed while the shortcut was waiting")
        require_bound_claim(state, claim)
        with INPUT_LOCK, file_guard(INPUT_FILE):
            result = dbus_call("InjectKeys", state["token"], json.dumps(request, separators=(",", ":")))
    return {
        "window": window_summary(state["target"]),
        "transaction": bounded_json_value(result),
        "global_seat_used": True,
    }


def renew_focus_lease_for_claim(claim: dict[str, Any]) -> None:
    with file_guard(LOCK_FILE):
        lease = load_lease()
        if not lease:
            return
        target_id = lease_window_id(lease)
        claim_window_id = str((claim.get("window") or {}).get("id") or "")
        if target_id != claim_window_id:
            return
        bound_token = str(lease.get("claim_token") or "")
        claim_token = str(claim.get("claim_token") or "")
        if not bound_token:
            raise RuntimeError(
                "the focus-lease target is reserved by an unclaimed lease; end or recover it first"
            )
        if (
            not secrets.compare_digest(bound_token, claim_token)
            or lease.get("owner_thread_id") != claim.get("owner_thread_id")
        ):
            raise RuntimeError(
                "the focus-lease target remains reserved by an older claim; end or recover it first"
            )
        dbus_call("RenewLease", lease["token"], f"{float(claim['lease_seconds']):.6f}")
        lease["broker"] = BROKER_IDENTITY
        save_lease(lease)


def claim_window(arguments: dict[str, Any], owner: str | None) -> dict[str, Any]:
    integration = shell_status()
    require_claimed_lease_support(integration)
    selected, shell_instance = resolve_window_for_shell(
        arguments.get("window"), integration["shell_instance"]
    )

    def renew_lease_and_validate_shell(claim: dict[str, Any]) -> None:
        renew_focus_lease_for_claim(claim)
        require_shell_instance(shell_instance)

    return CLAIMS.claim(
        selected,
        owner,
        arguments.get("lease_seconds", DEFAULT_LEASE_SECONDS),
        shell_instance,
        before_save=renew_lease_and_validate_shell,
    )


def release_window(arguments: dict[str, Any], owner: str | None) -> dict[str, Any]:
    shell = shell_status()

    def ensure_not_leased(claim: dict[str, Any]) -> None:
        with file_guard(LOCK_FILE):
            lease = load_lease()
            if (
                lease
                and lease_window_id(lease)
                == str((claim.get("window") or {}).get("id") or "")
            ):
                raise RuntimeError("end or recover the bound focus lease before releasing its window claim")

    return CLAIMS.release(
        arguments.get("claim_token"),
        owner,
        shell["shell_instance"],
        before_release=ensure_not_leased,
    )


def lease_target_id() -> str:
    state = load_lease()
    target = lease_window_id(state)
    if not target:
        raise RuntimeError("no focus lease with a valid target is active")
    return target


def end_lease(arguments: dict[str, Any], owner: str | None) -> dict[str, Any]:
    with file_guard(LOCK_FILE):
        with INPUT_LOCK, file_guard(INPUT_FILE):
            state = require_lease(str(arguments["lease_token"]), owner)
            recovery = state.get("broker") not in (None, BROKER_IDENTITY)
            return restore_lease(state, recovery=recovery)


def recover_lease(
    owner: str | None,
    claim: dict[str, Any] | None,
    expected_token: str | None = None,
) -> dict[str, Any]:
    with file_guard(LOCK_FILE):
        with INPUT_LOCK, file_guard(INPUT_FILE):
            state = load_lease()
            if not state:
                return {"restored": True, "message": "no unfinished focus lease"}
            if expected_token is not None and state.get("token") != expected_token:
                raise RuntimeError("focus lease changed while recovery was waiting")
            lease_owner = state.get("owner_thread_id")
            if lease_owner is not None and lease_owner != owner:
                if claim is not None:
                    raise RuntimeError("another computer-use agent still owns the focus lease's live window claim")
                if state.get("claim_token") is None and broker_is_alive(state.get("broker")):
                    raise RuntimeError("the original unclaimed focus-lease broker is still running")
            if (
                lease_owner is None
                and state.get("broker") not in (None, BROKER_IDENTITY)
                and broker_is_alive(state.get("broker"))
            ):
                raise RuntimeError("the original unclaimed focus-lease broker is still running")
            return restore_lease(state, recovery=True)


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
            raw_integration = dbus_call("Status")
            integration = bounded_json_value(raw_integration) if isinstance(raw_integration, dict) else None
        except Exception as exc:
            error = bounded_text(exc, MAX_ERROR_TEXT_CHARS)
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
    claimed_leases_ready = ready and shell_supports_claimed_leases(integration)
    claim_count: int | None = None
    claim_error: str | None = None
    if integration:
        try:
            claim_count = len(CLAIMS.list(integration["shell_instance"]))
        except Exception as exc:
            claim_error = bounded_text(exc, MAX_ERROR_TEXT_CHARS)
    return {
        "desktop": bounded_text(desktop),
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
            "parallel_window_claims": claimed_leases_ready,
            "serialized_global_seat": ready,
        },
        "requirements": {
            **checks,
            "gnome_shell_extension": ready,
            "claimed_focus_lease_protocol": claimed_leases_ready,
            "background_semantic_actions": "provided by the separate computer-use-linux@codex-computer-use-linux AT-SPI plugin; broker claims are policy coordination there, not a mechanical fence",
        },
        "active_lease": load_lease() is not None,
        "active_window_claims": claim_count,
        "claim_journal_error": claim_error,
        "session_identity": SESSION_IDENTITY,
        "safety_note": "Mutter exposes one global seat; lease input can visibly contend with physical input and cannot detect every held hardware button.",
    }


def text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}], "structuredContent": value, "isError": False}


def page_arguments(arguments: dict[str, Any], maximum: int) -> tuple[int, int | None, bool]:
    requested_limit = arguments.get("limit")
    if requested_limit is not None and (
        isinstance(requested_limit, bool)
        or not isinstance(requested_limit, int)
        or not 1 <= requested_limit <= maximum
    ):
        raise ValueError(f"limit must be an integer between 1 and {maximum}")
    cursor = arguments.get("cursor")
    if cursor is None:
        offset = 0
    elif isinstance(cursor, str) and len(cursor) <= 20 and cursor.isascii() and cursor.isdigit():
        offset = int(cursor)
    else:
        raise ValueError("cursor must be the next_cursor string from a previous result")
    return offset, requested_limit, requested_limit is not None or cursor is not None


def call_tool(name: str, arguments: dict[str, Any], owner: str | None = None) -> dict[str, Any]:
    if name == "session_status":
        return text_result(status())
    if name == "list_session_windows":
        offset, requested_limit, paginated = page_arguments(arguments, MAX_WINDOWS_PER_PAGE)
        listed = [window_summary(window) for window in windows()]
        limit = requested_limit if requested_limit is not None else (MAX_WINDOWS_PER_PAGE if paginated else len(listed))
        page: list[dict[str, Any]] = []
        end = offset
        while end < len(listed) and len(page) < limit:
            candidate = [*page, listed[end]]
            candidate_end = end + 1
            candidate_result = {
                "windows": candidate,
                "next_cursor": str(candidate_end) if candidate_end < len(listed) else None,
                "stable_id_lifetime": "until GNOME Shell or the window restarts",
            }
            encoded = json.dumps(candidate_result, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_WINDOW_RESULT_BYTES:
                break
            page = candidate
            end = candidate_end
        if not paginated and end < len(listed):
            raise RuntimeError("window list exceeds the model-visible result size; retry with limit to paginate")
        if not page and offset < len(listed):
            raise RuntimeError("a window entry exceeds the bounded listing result size")
        return text_result({
            "windows": page,
            "next_cursor": str(end) if end < len(listed) else None,
            "stable_id_lifetime": "until GNOME Shell or the window restarts",
        })
    if name == "claim_session_window":
        result = claim_window(arguments, owner)
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_CLAIM_RESULT_BYTES:
            raise RuntimeError("window claim result exceeds the bounded model-visible result size")
        return text_result(result)
    if name == "release_session_window":
        return text_result(release_window(arguments, owner))
    if name == "list_window_claims":
        shell = shell_status()
        offset, requested_limit, paginated = page_arguments(arguments, MAX_CLAIMS_PER_PAGE)
        listed = sorted(
            CLAIMS.list(shell["shell_instance"]),
            key=lambda claim: str((claim.get("window") or {}).get("id") or ""),
        )
        limit = requested_limit if requested_limit is not None else (MAX_CLAIMS_PER_PAGE if paginated else len(listed))
        page: list[dict[str, Any]] = []
        end = offset
        while end < len(listed) and len(page) < limit:
            candidate = [*page, listed[end]]
            candidate_end = end + 1
            candidate_result = {
                "claims": candidate,
                "next_cursor": str(candidate_end) if candidate_end < len(listed) else None,
                "truncated": candidate_end < len(listed),
            }
            encoded = json.dumps(candidate_result, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_CLAIM_RESULT_BYTES:
                break
            page = candidate
            end = candidate_end
        if not paginated and end < len(listed):
            raise RuntimeError("window claim list exceeds the model-visible result size; retry with limit to paginate")
        if not page and offset < len(listed):
            raise RuntimeError("a window claim entry exceeds the bounded listing result size")
        return text_result({
            "claims": page,
            "next_cursor": str(end) if end < len(listed) else None,
            "truncated": end < len(listed),
        })
    if name in {"capture_session_window", "get_session_window_capture", "save_session_window_capture"}:
        if name == "get_session_window_capture" and arguments.get("save_path") not in (None, ""):
            raise ValueError(
                "get_session_window_capture does not accept save_path; use save_session_window_capture to write a PNG"
            )
        if name == "save_session_window_capture" and (
            not isinstance(arguments.get("save_path"), str)
            or not arguments["save_path"]
            or len(arguments["save_path"]) > 4096
        ):
            raise ValueError("save_path must be a non-empty string of at most 4096 characters")
        selected, shell_instance = resolve_window_for_shell(arguments.get("window"))
        with CLAIMS.authorize(
            str(selected["id"]),
            owner,
            arguments.get("claim_token"),
            shell_instance,
            on_complete=renew_focus_lease_for_claim,
        ) as claim:
            if claim:
                require_claimed_lease_support(require_shell_instance(shell_instance))
            with file_guard(LOCK_FILE):
                with INPUT_LOCK, file_guard(INPUT_FILE):
                    return capture_window(
                        arguments,
                        owner,
                        selected,
                        claim,
                        expected_shell_instance=shell_instance,
                    )
    if name == "begin_focus_lease":
        selected, shell_instance = resolve_window_for_shell(arguments.get("window"))
        with CLAIMS.authorize(
            str(selected["id"]),
            owner,
            arguments.get("claim_token"),
            shell_instance,
            on_complete=renew_focus_lease_for_claim,
        ) as claim:
            if claim:
                require_claimed_lease_support(require_shell_instance(shell_instance))
            with file_guard(LOCK_FILE):
                with INPUT_LOCK, file_guard(INPUT_FILE):
                    return text_result(
                        begin_lease(
                            arguments,
                            owner,
                            selected,
                            claim,
                            expected_shell_instance=shell_instance,
                        )
                    )
    if name in {"lease_pointer_click", "lease_pointer_scroll", "lease_pointer_drag"}:
        action = name.removeprefix("lease_pointer_")
        target = lease_target_id()
        shell = shell_status()
        with CLAIMS.authorize(
            target,
            owner,
            arguments.get("claim_token"),
            shell["shell_instance"],
            on_complete=renew_focus_lease_for_claim,
        ) as claim:
            if claim:
                require_claimed_lease_support(shell)
            return text_result(pointer_action(arguments, action, owner, claim, target))
    if name == "send_lease_shortcut":
        target = lease_target_id()
        shell = shell_status()
        with CLAIMS.authorize(
            target,
            owner,
            arguments.get("claim_token"),
            shell["shell_instance"],
            on_complete=renew_focus_lease_for_claim,
        ) as claim:
            if claim:
                require_claimed_lease_support(shell)
            return text_result(send_shortcut(arguments, owner, claim, target))
    if name == "end_focus_lease":
        return text_result(end_lease(arguments, owner))
    if name == "recover_focus_lease":
        with file_guard(LOCK_FILE):
            snapshot = load_lease()
        if not snapshot:
            return text_result({"restored": True, "recovery_complete": True, "message": "no unfinished focus lease"})
        target = lease_window_id(snapshot)
        if not target:
            raise RuntimeError("focus lease journal has no valid target")
        shell = shell_status()
        with CLAIMS.inspect(target, shell["shell_instance"]) as claim:
            if claim:
                require_claimed_lease_support(shell)
            return text_result(recover_lease(owner, claim, str(snapshot.get("token") or "")))
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
                "instructions": "Operate the real GNOME session. Claim one window per parallel agent, treat claims as cooperative policy for the separate AT-SPI process, and use this broker's serialized acknowledged focus-lease lane for capture or global-seat input.",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("tools/call params must be an object")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            result = call_tool(
                str(params.get("name") or ""), arguments, owner_from_params(params)
            )
        elif method == "ping":
            result = {}
        else:
            message = bounded_text(f"method not found: {method}", MAX_ERROR_TEXT_CHARS)
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": message}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        message = bounded_text(exc, MAX_ERROR_TEXT_CHARS)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}}


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
                message = bounded_text(exc, MAX_ERROR_TEXT_CHARS)
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": message}}), flush=True)
            continue
        worker = threading.Thread(target=process, args=(message,), daemon=True)
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    return 0
