import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser() / "same-session-computer-use"
PLUGIN_LOCK_FILE = STATE_DIR / "plugin-build-load.lock"
PLUGIN_PACKAGES = ("pixman-1", "libdrm", "hyprland", "libinput", "libudev", "wayland-server", "xkbcommon")


def run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


@contextmanager
def file_guard(path: Path):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def plugin_build_requirements() -> dict[str, bool]:
    compiler = shlex.split(os.environ.get("CXX", "g++"))
    pkg_config = shutil.which("pkg-config") is not None
    headers = pkg_config and run(["pkg-config", "--exists", *PLUGIN_PACKAGES]).returncode == 0
    return {
        "python_3_10_or_newer": sys.version_info >= (3, 10),
        "compiler": bool(compiler and shutil.which(compiler[0])),
        "pkg_config": pkg_config,
        "hyprland_development_headers": headers,
        "plugin_source": (ROOT / "hyprland/target-pointer.cpp").is_file(),
    }


def plugin_cache_directory(version: str, source: bytes) -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    key = hashlib.sha256(version.encode() + b"\0" + source).hexdigest()
    return cache_home / "same-session-computer-use/hyprland" / key


def build_target_pointer_plugin(version: str) -> Path:
    source = ROOT / "hyprland/target-pointer.cpp"
    source_bytes = source.read_bytes()
    directory = plugin_cache_directory(version, source_bytes)
    library = directory / "same-session-target-pointer.so"
    if library.is_file():
        return library

    requirements = plugin_build_requirements()
    missing = [name for name, available in requirements.items() if not available]
    if missing:
        raise RuntimeError(f"cannot build targeted-pointer plugin; missing requirements: {', '.join(missing)}")
    cflags = run(["pkg-config", "--cflags", *PLUGIN_PACKAGES])
    if cflags.returncode:
        raise RuntimeError(cflags.stderr.strip() or "failed to resolve Hyprland compiler flags")

    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{library.name}.{os.getpid()}.tmp"
    compiler = shlex.split(os.environ.get("CXX", "g++"))
    flags = ["-O2", "-shared", "-fPIC", "-std=c++23", "-Wall", "-Wextra", "-Wpedantic"]
    if Path(compiler[-1]).name == "g++":
        flags.append("--no-gnu-unique")
    try:
        built = subprocess.run(
            [*compiler, *flags, *shlex.split(cflags.stdout), str(source), "-o", str(temporary)],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if built.returncode:
            raise RuntimeError(built.stderr.strip() or built.stdout.strip() or "failed to build targeted-pointer plugin")
        temporary.replace(library)
    finally:
        temporary.unlink(missing_ok=True)
    return library


def ensure_target_pointer_plugin() -> None:
    with file_guard(PLUGIN_LOCK_FILE):
        listed = run(["hyprctl", "plugin", "list"])
        if listed.returncode == 0 and "same-session-target-pointer" in listed.stdout:
            return
        version = run(["hyprctl", "version"])
        if version.returncode:
            raise RuntimeError(version.stderr.strip() or "failed to read Hyprland version")
        library = build_target_pointer_plugin(version.stdout)
        loaded = run(["hyprctl", "plugin", "load", str(library)], timeout=20)
        if loaded.returncode or "ok" not in loaded.stdout.lower():
            raise RuntimeError(loaded.stderr.strip() or loaded.stdout.strip() or "failed to load targeted-pointer plugin")


def native_input_status() -> dict[str, Any]:
    ensure_target_pointer_plugin()
    proc = run(["hyprctl", "-j", "cutargetstatus"])
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "native input safety probe failed")
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("native input safety probe returned invalid JSON") from exc
    if not isinstance(status, dict) or status.get("ok") is not True:
        raise RuntimeError(str(status.get("error") if isinstance(status, dict) else "native input safety probe failed"))
    return status


def ensure_native_input_safe() -> dict[str, Any]:
    status = native_input_status()
    if status.get("safe_to_inject") is not True:
        blocked = [
            name
            for name in ("session_locked", "held_buttons", "pointer_constrained", "pointer_locked", "dnd_active")
            if status.get(name) is True
        ]
        if status.get("pointer_seat") is not True:
            blocked.append("no_pointer_seat")
        reason = ", ".join(blocked) or "unknown safety condition"
        raise RuntimeError(f"native input safety probe refused input: {reason}")
    return status
