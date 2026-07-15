import base64
import hashlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "x11/x11-window-capture.c"
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-x11-background-computer-use"
MAX_CAPTURE_BYTES = 16 * 1024 * 1024


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


def capture_window(window: dict[str, Any], requested: Any) -> dict[str, Any]:
    if window["minimized"] or not window["mapped"]:
        raise RuntimeError("exact XComposite capture requires a mapped, non-minimized window")
    if requested:
        destination = Path(str(requested)).expanduser()
        if not destination.is_absolute():
            raise ValueError("save_path must be absolute")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    else:
        destination = None
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
        image_size = png_size(raw)
        if image_size is None:
            raise RuntimeError("exact XComposite window capture returned an invalid PNG")
        if destination:
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = {
        "window": window,
        "saved_to": str(destination) if destination else None,
        "coordinate_space": {
            "window_local": {"width": window["width"], "height": window["height"]},
            "screenshot_pixels": image_size,
        },
        "capture_backend": "XComposite named window pixmap",
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
