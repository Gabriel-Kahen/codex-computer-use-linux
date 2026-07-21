import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser() / "same-session-computer-use"
PLUGIN_LOCK_FILE = STATE_DIR / "plugin-build-load.lock"
PLUGIN_PACKAGES = ("pixman-1", "libdrm", "hyprland", "libinput", "libudev", "wayland-server", "xkbcommon")
NATIVE_PLUGIN_VERSION = "0.1.3"
_IDENTITY_CACHE_LOCK = threading.Lock()
_IDENTITY_INIT_LOCK = threading.Lock()
_IDENTITY_CACHE: dict[tuple[str, str], dict[str, str]] = {}


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


def plugin_identity(version: str, source: bytes) -> dict[str, str]:
    return {
        "plugin_version": NATIVE_PLUGIN_VERSION,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "hyprland_build_sha256": hashlib.sha256(version.encode()).hexdigest(),
    }


def plugin_session_key() -> tuple[str, str]:
    instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    return instance, str(runtime / wayland)


def _cache_plugin_identity(identity: dict[str, str]) -> None:
    immutable = {
        name: identity[name]
        for name in ("plugin_version", "source_sha256", "hyprland_build_sha256")
    }
    with _IDENTITY_CACHE_LOCK:
        _IDENTITY_CACHE[plugin_session_key()] = immutable


def invalidate_plugin_identity() -> None:
    with _IDENTITY_CACHE_LOCK:
        _IDENTITY_CACHE.pop(plugin_session_key(), None)


def native_action_identity() -> dict[str, str]:
    with _IDENTITY_CACHE_LOCK:
        cached = _IDENTITY_CACHE.get(plugin_session_key())
        if cached is not None:
            return dict(cached)
    with _IDENTITY_INIT_LOCK:
        with _IDENTITY_CACHE_LOCK:
            cached = _IDENTITY_CACHE.get(plugin_session_key())
            if cached is not None:
                return dict(cached)
        status = ensure_target_pointer_plugin()
        identity = {
            name: str(status[name])
            for name in (
                "plugin_version",
                "source_sha256",
                "hyprland_build_sha256",
            )
        }
        _cache_plugin_identity(identity)
        return identity


def plugin_identity_token(identity: dict[str, str]) -> str:
    return "v1.{plugin_version}.{source_sha256}.{hyprland_build_sha256}".format(
        **identity
    )


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
    identity = plugin_identity(version, source_bytes)
    flags.extend(
        f'-DCU_{name.upper()}="{value}"' for name, value in identity.items()
    )
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


def _native_input_status() -> dict[str, Any]:
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


def _validate_plugin_identity(
    status: dict[str, Any], expected: dict[str, str]
) -> None:
    actual = {name: status.get(name) for name in expected}
    if actual != expected:
        raise RuntimeError(
            "loaded same-session-target-pointer identity does not match this broker: "
            f"expected={json.dumps(expected, sort_keys=True)}, "
            f"actual={json.dumps(actual, sort_keys=True)}; unload the stale plugin and retry"
        )
    if status.get("hyprland_build_abi") != status.get("hyprland_runtime_abi"):
        raise RuntimeError("loaded same-session-target-pointer Hyprland ABI does not match the runtime")


def ensure_target_pointer_plugin() -> dict[str, Any]:
    with file_guard(PLUGIN_LOCK_FILE):
        version = run(["hyprctl", "version"])
        if version.returncode:
            raise RuntimeError(version.stderr.strip() or "failed to read Hyprland version")
        source = (ROOT / "hyprland/target-pointer.cpp").read_bytes()
        expected = plugin_identity(version.stdout, source)
        listed = run(["hyprctl", "plugin", "list"])
        if listed.returncode == 0 and "same-session-target-pointer" in listed.stdout:
            status = _native_input_status()
            _validate_plugin_identity(status, expected)
            _cache_plugin_identity(expected)
            return status
        library = build_target_pointer_plugin(version.stdout)
        loaded = run(["hyprctl", "plugin", "load", str(library)], timeout=20)
        if loaded.returncode or "ok" not in loaded.stdout.lower():
            raise RuntimeError(loaded.stderr.strip() or loaded.stdout.strip() or "failed to load targeted-pointer plugin")
        status = _native_input_status()
        _validate_plugin_identity(status, expected)
        _cache_plugin_identity(expected)
        return status


def native_input_status() -> dict[str, Any]:
    return ensure_target_pointer_plugin()


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


def run_target_pointer_action(action: str, arguments: list[str]) -> dict[str, Any]:
    identity = native_action_identity()

    def dispatch() -> subprocess.CompletedProcess[str]:
        return run(
            [
                "hyprctl",
                "-j",
                "cutarget",
                action,
                plugin_identity_token(identity),
                *arguments,
            ],
            timeout=20,
        )

    proc = dispatch()
    unavailable = "unknown request" in f"{proc.stdout}\n{proc.stderr}".lower()
    if unavailable:
        invalidate_plugin_identity()
        status = ensure_target_pointer_plugin()
        identity = {
            name: str(status[name])
            for name in (
                "plugin_version",
                "source_sha256",
                "hyprland_build_sha256",
            )
        }
        proc = dispatch()
    if proc.returncode:
        raise RuntimeError(
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"Wayland targeted {action} failed"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Wayland targeted {action} returned invalid transaction JSON"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Wayland targeted {action} returned a non-object transaction")
    if result.get("ok") is not True:
        error = str(result.get("error") or f"Wayland targeted {action} failed")
        if "identity" in error.lower() or "abi" in error.lower():
            invalidate_plugin_identity()
        raise RuntimeError(error)
    actual_identity = result.get("identity")
    if not isinstance(actual_identity, dict):
        raise RuntimeError("native input transaction omitted its plugin identity")
    _validate_plugin_identity(actual_identity, identity)
    return result
