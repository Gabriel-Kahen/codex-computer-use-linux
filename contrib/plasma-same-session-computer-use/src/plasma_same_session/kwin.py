import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser() / "plasma-same-session-computer-use"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser() / "plasma-same-session-computer-use"


def run(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


@contextmanager
def file_guard(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a+") as handle:
        path.chmod(0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def kdotool(*args: str) -> str:
    if shutil.which("kdotool") is None:
        raise RuntimeError("kdotool is required for KWin window identity and exact capture selection")
    proc = run(["kdotool", *args])
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"kdotool {' '.join(args)} failed")
    return proc.stdout.strip()


def _geometry(value: str) -> dict[str, int]:
    position = re.search(r"Position:\s*(-?\d+),(-?\d+)", value)
    size = re.search(r"Geometry:\s*(\d+)x(\d+)", value)
    if not position or not size:
        raise RuntimeError(f"unrecognized kdotool geometry: {value!r}")
    return {"x": int(position[1]), "y": int(position[2]), "width": int(size[1]), "height": int(size[2])}


def active_window_id() -> str | None:
    try:
        value = kdotool("getactivewindow")
    except RuntimeError:
        return None
    return value.splitlines()[-1].strip() if value else None


def current_desktop() -> int:
    return int(kdotool("get_desktop").splitlines()[-1])


def pointer_position() -> dict[str, int] | None:
    try:
        value = kdotool("getmouselocation", "--shell")
    except RuntimeError:
        return None
    fields = dict(line.split("=", 1) for line in value.splitlines() if "=" in line)
    if "X" not in fields or "Y" not in fields:
        return None
    return {"x": int(fields["X"]), "y": int(fields["Y"])}


def window_desktop(window_id: str, info: dict[str, Any] | None = None) -> int | None:
    try:
        value = kdotool("get_desktop_for_window", window_id)
        return int(value)
    except (RuntimeError, ValueError):
        details = info if info is not None else window_info(window_id)
        return -1 if "desktops" in details and not details["desktops"] else None


def window_info(window_id: str) -> dict[str, Any]:
    command = shutil.which("qdbus6") or shutil.which("qdbus")
    if not command:
        return {}
    proc = run([command, "org.kde.KWin", "/KWin", "org.kde.KWin.getWindowInfo", window_id.strip("{}")])
    if proc.returncode:
        return {}
    values = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def window_boolean(window_id: str, property_name: str) -> bool | None:
    return {"true": True, "false": False}.get(window_info(window_id).get(property_name, "").lower())


def list_windows() -> list[dict[str, Any]]:
    active = active_window_id()
    ids = [line.strip() for line in kdotool("search", ".").splitlines() if line.strip()]
    windows = []
    for window_id in ids:
        try:
            geometry = _geometry(kdotool("getwindowgeometry", window_id))
            info = window_info(window_id)
            windows.append({
                "id": window_id,
                "capture_id": window_id.strip("{}"),
                "title": kdotool("getwindowname", window_id),
                "class": kdotool("getwindowclassname", window_id),
                "pid": int(kdotool("getwindowpid", window_id)),
                "desktop": window_desktop(window_id, info),
                "active": window_id == active,
                "minimized": {"true": True, "false": False}.get(info.get("minimized", "").lower()),
                "fullscreen": {"true": True, "false": False}.get(info.get("fullscreen", "").lower()),
                "excluded_from_capture": {"true": True, "false": False}.get(info.get("excludeFromCapture", "").lower()),
                "geometry": geometry,
            })
        except (RuntimeError, ValueError):
            continue
    return windows


def resolve_window(query: str) -> dict[str, Any]:
    windows = list_windows()
    lowered = query.lower().strip()
    exact = [w for w in windows if lowered in {w["id"].lower(), w["capture_id"].lower(), w["class"].lower()}]
    matches = exact or [w for w in windows if lowered in w["title"].lower()]
    if not matches:
        raise RuntimeError(f"no KWin window matches {query!r}")
    if len(matches) > 1:
        choices = ", ".join(f"{w['class']} {w['title']} ({w['id']})" for w in matches[:8])
        raise RuntimeError(f"window query is ambiguous; use its KWin id: {choices}")
    return matches[0]


def activate(window_id: str) -> None:
    kdotool("windowactivate", window_id)


def set_desktop(desktop: int) -> None:
    kdotool("set_desktop", str(desktop))


def set_window_desktop(window_id: str, desktop: int) -> None:
    kdotool("set_desktop_for_window", window_id, "all" if desktop == -1 else str(desktop))


def set_window_minimized(window_id: str, minimized: bool) -> None:
    kdotool("windowstate", "--add" if minimized else "--remove", "minimized", window_id)


def screen_locked() -> bool | None:
    proc = run([
        "gdbus", "call", "--session", "--dest", "org.freedesktop.ScreenSaver",
        "--object-path", "/ScreenSaver", "--method", "org.freedesktop.ScreenSaver.GetActive",
    ])
    if proc.returncode:
        return None
    if "true" in proc.stdout.lower():
        return True
    if "false" in proc.stdout.lower():
        return False
    return None


def helper_requirements() -> dict[str, bool]:
    qt6 = shutil.which("pkg-config") is not None and run(
        ["pkg-config", "--exists", "Qt6Core", "Qt6Gui", "Qt6DBus"]
    ).returncode == 0
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    return {
        "kdotool": shutil.which("kdotool") is not None,
        "gdbus": shutil.which("gdbus") is not None,
        "qdbus": bool(shutil.which("qdbus6") or shutil.which("qdbus")),
        "cxx": bool(compiler and shutil.which(compiler[0])),
        "pkg_config": shutil.which("pkg-config") is not None,
        "qt6_development_files": qt6,
        "capture_helper_source": (ROOT / "kwin/capture-helper.cpp").is_file(),
    }


def build_capture_helper() -> Path:
    source = ROOT / "kwin/capture-helper.cpp"
    if run(["pkg-config", "--exists", "Qt6Core", "Qt6Gui", "Qt6DBus"]).returncode:
        raise RuntimeError("Qt 6 Core, Gui, and DBus development files are required for exact KWin capture")
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    directory = CACHE_DIR / "capture-helper" / key
    helper = directory / "plasma-same-session-capture"
    if helper.is_file() and os.access(helper, os.X_OK):
        install_capture_desktop_file(helper)
        return helper
    directory.mkdir(parents=True, exist_ok=True)
    flags = run(["pkg-config", "--cflags", "--libs", "Qt6Core", "Qt6Gui", "Qt6DBus"])
    if flags.returncode:
        raise RuntimeError(flags.stderr.strip() or "failed to resolve Qt build flags")
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    if not compiler:
        raise RuntimeError("CXX does not name a compiler")
    temporary = helper.with_suffix(f".{os.getpid()}.tmp")
    proc = run(
        [*compiler, "-O2", "-fPIC", "-std=c++17", str(source), "-o", str(temporary), *shlex.split(flags.stdout)],
        timeout=60,
    )
    if proc.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "failed to build KWin capture helper")
    temporary.chmod(0o755)
    temporary.replace(helper)
    install_capture_desktop_file(helper)
    return helper


def install_capture_desktop_file(helper: Path) -> None:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
    desktop_file = data_home / "applications/plasma-same-session-capture.desktop"
    escaped_helper = str(helper).replace("\\", "\\\\").replace('"', '\\"')
    contents = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Plasma Same Session Capture",
        "NoDisplay=true",
        f'Exec="{escaped_helper}" %U',
        "X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2",
        "",
    ])
    if not desktop_file.is_file() or desktop_file.read_text() != contents:
        desktop_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = desktop_file.with_suffix(".tmp")
        temporary.write_text(contents)
        temporary.replace(desktop_file)
        updater = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
        if updater:
            run([updater], timeout=30)


def capture_window(window_id: str, output: Path) -> None:
    with file_guard(STATE_DIR / "capture-helper.lock"):
        helper = build_capture_helper()
    proc = run([str(helper), window_id.strip("{}"), str(output)], timeout=30)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "KWin exact window capture failed")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("KWin capture helper produced no image")
    marker = STATE_DIR / "exact-capture-authorized"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.parent.chmod(0o700)
    marker.write_text(json.dumps(_session_identity()))
    marker.chmod(0o600)


def _session_identity() -> dict[str, Any]:
    display = os.environ.get("WAYLAND_DISPLAY")
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    socket_identity = None
    if display and runtime_dir:
        try:
            socket = (Path(runtime_dir) / display).stat()
            socket_identity = {"device": socket.st_dev, "inode": socket.st_ino}
        except OSError:
            pass
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = None
    return {
        "uid": os.getuid(),
        "boot_id": boot_id,
        "wayland_display": display,
        "wayland_socket": socket_identity,
        "session_id": os.environ.get("XDG_SESSION_ID"),
    }


def capture_authorized_in_current_session() -> bool:
    marker = STATE_DIR / "exact-capture-authorized"
    try:
        value = json.loads(marker.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    current = _session_identity()
    if not current["session_id"] and not current["wayland_socket"]:
        return False
    return value == current
