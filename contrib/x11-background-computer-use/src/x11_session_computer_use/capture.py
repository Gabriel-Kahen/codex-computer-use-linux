import hashlib
import fcntl
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "x11/x11-window-capture.c"
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-x11-background-computer-use"


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
