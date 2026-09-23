import atexit
import base64
import hashlib
import fcntl
import json
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "x11/x11-window-capture.c"
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-x11-background-computer-use"
MAX_CAPTURE_BYTES = 5 * 1024 * 1024
MAX_NATIVE_HEADER_BYTES = 16 * 1024
NATIVE_REQUEST_TIMEOUT = 20.0


class NativeTransportUnavailable(RuntimeError):
    pass


_native_lock = threading.Lock()
_native_process: subprocess.Popen[bytes] | None = None
_native_owner_pid: int | None = None


def _native_launcher() -> Path | None:
    override = os.environ.get("CODEX_X11_NATIVE_CAPTURE_HELPER")
    candidates = [
        Path(override).expanduser() if override else None,
        ROOT.parent.parent / "computer-use-linux/bin/codex-computer-use",
    ]
    installed = shutil.which("computer-use-linux")
    if installed:
        candidates.append(Path(installed))
    return next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


def native_transport_available() -> bool:
    return (
        bool(os.environ.get("DISPLAY"))
        and os.environ.get("CODEX_X11_NATIVE_CAPTURE", "1") != "0"
        and _native_launcher() is not None
    )


def _stop_native_transport() -> None:
    global _native_owner_pid, _native_process
    process, _native_process = _native_process, None
    owner_pid, _native_owner_pid = _native_owner_pid, None
    if owner_pid != os.getpid():
        return
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _start_native_transport() -> subprocess.Popen[bytes]:
    global _native_owner_pid, _native_process
    if _native_owner_pid is not None and _native_owner_pid != os.getpid():
        # A fork inherited the file descriptors, but it must not share a
        # request stream or terminate the parent's worker.
        inherited = _native_process
        if inherited is not None:
            for stream in (inherited.stdin, inherited.stdout):
                if stream is not None:
                    stream.close()
        _native_process = None
        _native_owner_pid = None
    if _native_process is not None and _native_process.poll() is None:
        return _native_process
    if not os.environ.get("DISPLAY") or os.environ.get("CODEX_X11_NATIVE_CAPTURE", "1") == "0":
        raise NativeTransportUnavailable("native X11 capture transport is disabled or DISPLAY is unset")
    launcher = _native_launcher()
    if launcher is None:
        raise NativeTransportUnavailable("the shipped computer-use-linux capture worker is unavailable")
    try:
        _native_process = subprocess.Popen(
            [str(launcher), "x11-capture-worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        _native_owner_pid = os.getpid()
    except OSError as error:
        raise NativeTransportUnavailable(f"failed to start native X11 capture transport: {error}") from error
    return _native_process


def _read_with_deadline(stream: Any, size: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not select.select([stream], [], [], timeout)[0]:
            raise NativeTransportUnavailable("native X11 capture transport timed out")
        chunk = os.read(stream.fileno(), size - len(result))
        if not chunk:
            raise NativeTransportUnavailable("native X11 capture transport closed unexpectedly")
        result.extend(chunk)
    return bytes(result)


def _readline_with_deadline(stream: Any, deadline: float) -> bytes:
    result = bytearray()
    while len(result) <= MAX_NATIVE_HEADER_BYTES:
        byte = _read_with_deadline(stream, 1, deadline)
        result.extend(byte)
        if byte == b"\n":
            return bytes(result)
    raise NativeTransportUnavailable("native X11 capture response header is too large")


def _native_request(request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    with _native_lock:
        process = _start_native_transport()
        if process.stdin is None or process.stdout is None:
            _stop_native_transport()
            raise NativeTransportUnavailable("native X11 capture transport pipes are unavailable")
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > 4096:
            raise ValueError("native X11 capture request is too large")
        deadline = time.monotonic() + NATIVE_REQUEST_TIMEOUT
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
            header = json.loads(_readline_with_deadline(process.stdout, deadline))
            if (
                not isinstance(header, dict)
                or type(header.get("protocol")) is not int
                or header["protocol"] != 1
                or type(header.get("ok")) is not bool
                or isinstance(header.get("bytes"), bool)
                or not isinstance(header.get("bytes"), int)
                or not 0 <= header["bytes"] <= MAX_CAPTURE_BYTES
            ):
                raise NativeTransportUnavailable("native X11 capture transport returned an invalid header")
            raw = _read_with_deadline(process.stdout, header["bytes"], deadline) if header["bytes"] else b""
        except (
            NativeTransportUnavailable,
            BrokenPipeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            _stop_native_transport()
            if isinstance(error, NativeTransportUnavailable):
                raise
            raise NativeTransportUnavailable(f"native X11 capture transport failed: {error}") from error
        if not header.get("ok"):
            raise RuntimeError(str(header.get("error") or "native X11 capture failed"))
        return header, raw


def authenticated_pid(xid: str) -> int | None:
    try:
        numeric_xid = int(xid, 0)
    except ValueError:
        return None
    try:
        header, _ = _native_request({"op": "pid", "window_id": numeric_xid})
    except NativeTransportUnavailable:
        proc = subprocess.run(
            [str(ensure_capture_helper()), "--pid", xid],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else None
    except RuntimeError:
        return None
    pid = header.get("authenticated_pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def build_requirements() -> dict[str, bool]:
    return {
        "cc": shutil.which(os.environ.get("CC", "cc")) is not None,
        "pkg-config": shutil.which("pkg-config") is not None,
        "x11+xcomposite+xres+libpng development files": subprocess.run(
            ["pkg-config", "--exists", "x11", "xcomposite", "xres", "libpng"],
            capture_output=True,
            check=False,
        ).returncode == 0 if shutil.which("pkg-config") else False,
    }


def ensure_capture_helper() -> Path:
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    target = CACHE_ROOT / digest / "x11-window-capture"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_file() and os.access(target, os.X_OK):
            return target
        requirements = build_requirements()
        missing = [name for name, available in requirements.items() if not available]
        if missing:
            raise RuntimeError(
                "cannot build exact XComposite capture helper; missing " + ", ".join(missing)
            )
        flags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "x11", "xcomposite", "xres", "libpng"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split()
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        proc = subprocess.run(
            [os.environ.get("CC", "cc"), "-O2", "-Wall", "-Wextra", "-o", str(temporary), str(SOURCE), *flags],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(proc.stderr.strip() or "failed to build XComposite capture helper")
        temporary.chmod(0o755)
        temporary.replace(target)
    return target


def png_size(raw: bytes) -> dict[str, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    return {"width": width, "height": height} if width and height else None


def _legacy_capture(window: dict[str, Any]) -> bytes:
    fd, name = tempfile.mkstemp(prefix="x11-window-", suffix=".png")
    os.close(fd)
    temporary = Path(name)
    try:
        proc = subprocess.run(
            [str(ensure_capture_helper()), window["xid"], str(temporary)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "exact XComposite window capture failed")
        capture_bytes = temporary.stat().st_size
        if capture_bytes > MAX_CAPTURE_BYTES:
            raise RuntimeError(
                f"exact XComposite window capture is {capture_bytes} bytes; "
                f"maximum transport size is {MAX_CAPTURE_BYTES} bytes"
            )
        raw = temporary.read_bytes()
        if len(raw) > MAX_CAPTURE_BYTES:
            raise RuntimeError(
                f"exact XComposite window capture is {len(raw)} bytes; "
                f"maximum transport size is {MAX_CAPTURE_BYTES} bytes"
            )
        return raw
    finally:
        temporary.unlink(missing_ok=True)


def _native_capture(window: dict[str, Any]) -> bytes:
    header, raw = _native_request(
        {
            "op": "capture",
            "window_id": int(window["xid"], 0),
            "expected_pid": window.get("pid"),
            "expected_width": window.get("width"),
            "expected_height": window.get("height"),
        }
    )
    if header.get("authenticated_pid") != window.get("pid"):
        raise RuntimeError("XRes PID identity changed during native X11 capture")
    if header.get("width") != window.get("width") or header.get("height") != window.get("height"):
        raise RuntimeError("X11 window bounds changed during native capture")
    return raw


def _install_capture(destination: Path, raw: bytes) -> None:
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def capture_window(window: dict[str, Any], requested: Any) -> dict[str, Any]:
    if window["minimized"] or not window["mapped"]:
        raise RuntimeError("exact XComposite capture requires a mapped, non-minimized window")
    if requested:
        destination = Path(str(requested)).expanduser()
        if not destination.is_absolute():
            raise ValueError("save_path must be absolute")
        destination.parent.mkdir(parents=True, exist_ok=True)
    else:
        destination = None
    try:
        raw = _native_capture(window)
        capture_backend = "persistent native XComposite/XRes transport"
    except NativeTransportUnavailable:
        raw = _legacy_capture(window)
        capture_backend = "XComposite named window pixmap"
    if len(raw) > MAX_CAPTURE_BYTES:
        raise RuntimeError(
            f"exact XComposite window capture is {len(raw)} bytes; "
            f"maximum transport size is {MAX_CAPTURE_BYTES} bytes"
        )
    image_size = png_size(raw)
    if image_size is None:
        raise RuntimeError("exact XComposite window capture returned an invalid PNG")
    if destination:
        _install_capture(destination, raw)
    metadata = {
        "window": window,
        "saved_to": str(destination) if destination else None,
        "coordinate_space": {
            "window_local": {"width": window["width"], "height": window["height"]},
            "screenshot_pixels": image_size,
        },
        "capture_backend": capture_backend,
        "focus_changed": False,
        "pointer_moved": False,
        "desktop_changed": False,
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps(metadata, indent=2)},
            {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/png"},
        ],
        "isError": False,
    }


atexit.register(_stop_native_transport)
