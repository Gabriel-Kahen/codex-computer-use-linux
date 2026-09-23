#!/usr/bin/env python3
"""Fetch an explicit, verified release without changing installed plugins."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import tarfile
import tempfile

REPOSITORY = "Gabriel-Kahen/codex-computer-use-linux"
WORKFLOW = f"{REPOSITORY}/.github/workflows/computer-use-prebuilt-release.yml"


def verify_checksum(archive: Path, checksum: Path):
    fields = checksum.read_text().split()
    if len(fields) != 2 or fields[1] != archive.name:
        raise ValueError("checksum file must describe exactly the selected archive")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if fields[0] != digest:
        raise ValueError("release archive checksum mismatch")


def unpack(archive: Path, destination: Path):
    if destination.exists():
        raise ValueError(f"destination already exists; retain it for rollback: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".computer-use-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "bundle"
        stage.mkdir()
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                parts = Path(member.name).parts
                if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
                    raise ValueError(f"unexpected archive entry: {member.name}")
            bundle.extractall(stage, filter="data")
        if not (stage / ".agents/plugins/marketplace.json").is_file():
            raise ValueError("release does not contain a plugin marketplace")
        stage.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="explicit release version, e.g. 0.6.0")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", args.version):
        parser.error("invalid release version")
    machine = platform.machine()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    if platform.system() != "Linux" or not arch:
        parser.error("prebuilt releases require Linux x86_64 or aarch64")
    target = f"{arch}-unknown-linux-gnu"
    filename = f"codex-computer-use-linux-{args.version}-{target}.tar.gz"
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    destination = (args.destination or data / "codex-computer-use-linux" / f"{args.version}-{target}").resolve()
    if destination.exists():
        parser.error(f"destination already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix="computer-use-download-") as tmp:
        download = Path(tmp)
        subprocess.run([
            "gh", "release", "download", f"computer-use-v{args.version}",
            "--repo", REPOSITORY, "--dir", tmp,
            "--pattern", filename, "--pattern", filename + ".sha256",
        ], check=True)
        archive = download / filename
        verify_checksum(archive, download / (filename + ".sha256"))
        subprocess.run([
            "gh", "attestation", "verify", str(archive), "--repo", REPOSITORY,
            "--signer-workflow", WORKFLOW,
        ], check=True)
        unpack(archive, destination)
    print(f"Verified release extracted to {destination}")
    print("For an existing installation, follow MIGRATION.md first. Then run:")
    print(f"codex plugin marketplace add {shlex.quote(str(destination))}")
    print("codex plugin add computer-use-linux@codex-computer-use-linux")
    print("Install the companion for your desktop and start a new Codex task.")


if __name__ == "__main__":
    main()
