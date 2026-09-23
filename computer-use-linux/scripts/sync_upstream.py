#!/usr/bin/env python3
"""Inspect or three-way merge the pinned computer-use-linux upstream."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = PLUGIN_ROOT / "UPSTREAM.toml"


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def config() -> dict:
    with PROVENANCE.open("rb") as handle:
        return tomllib.load(handle)


def remote_head(repository: str, ref: str) -> str:
    result = run("git", "ls-remote", repository, f"refs/heads/{ref}")
    line = result.stdout.decode().strip()
    if not line:
        raise RuntimeError(f"remote ref not found: {repository} {ref}")
    return line.split()[0]


def secondary_path_head(item: dict) -> str:
    with tempfile.TemporaryDirectory(prefix="cul-secondary-") as temp:
        repo = Path(temp)
        run("git", "init", "--quiet", repo.as_posix())
        run("git", "remote", "add", "origin", item["repository"], cwd=repo)
        run("git", "fetch", "--quiet", "--filter=blob:none", "origin", item["ref"], cwd=repo)
        result = run(
            "git",
            "log",
            "-1",
            "--format=%H",
            "FETCH_HEAD",
            "--",
            item["path"],
            cwd=repo,
        )
        return result.stdout.decode().strip()


def status() -> dict:
    data = config()
    primary = data["primary"]
    secondary = data["secondary"]
    primary_head = remote_head(primary["repository"], primary["ref"])
    secondary_head = secondary_path_head(secondary)
    return {
        "primary": {
            "name": primary["name"],
            "pinned": primary["rev"],
            "latest": primary_head,
            "outdated": primary_head != primary["rev"],
        },
        "secondary": {
            "name": secondary["name"],
            "pinned": secondary["rev"],
            "latest_path_change": secondary_head,
            "outdated": secondary_head != secondary["rev"],
        },
    }


def git_bytes(repo: Path, revision: str, path: str) -> bytes | None:
    result = run("git", "show", f"{revision}:{path}", cwd=repo, check=False)
    return result.stdout if result.returncode == 0 else None


def git_files(repo: Path, revision: str) -> set[str]:
    result = run("git", "ls-tree", "-r", "--name-only", revision, cwd=repo)
    return set(result.stdout.decode().splitlines())


def git_mode(repo: Path, revision: str, path: str) -> int:
    result = run("git", "ls-tree", revision, "--", path, cwd=repo)
    return 0o755 if result.stdout.startswith(b"100755") else 0o644


def write_file(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def merge_text(local: bytes, base: bytes, remote: bytes) -> tuple[bytes, bool]:
    with tempfile.TemporaryDirectory(prefix="cul-merge-") as temp:
        root = Path(temp)
        paths = [root / name for name in ("local", "base", "remote")]
        for path, content in zip(paths, (local, base, remote), strict=True):
            path.write_bytes(content)
        result = run(
            "git",
            "merge-file",
            "-p",
            paths[0].as_posix(),
            paths[1].as_posix(),
            paths[2].as_posix(),
            check=False,
        )
        if result.returncode not in range(128):
            raise RuntimeError(result.stderr.decode().strip())
        return result.stdout, result.returncode != 0


def update_pin(old: str, new: str, tree: str) -> None:
    text = PROVENANCE.read_text()
    old_rev = f'rev = "{old}"'
    old_tree = next(line for line in text.splitlines() if line.startswith("tree = "))
    if old_rev not in text:
        raise RuntimeError("primary revision changed while sync was running")
    text = text.replace(old_rev, f'rev = "{new}"', 1)
    text = text.replace(old_tree, f'tree = "{tree}"', 1)
    PROVENANCE.write_text(text)


def prepare(target_ref: str | None) -> None:
    primary = config()["primary"]
    old = primary["rev"]
    target_ref = target_ref or primary["ref"]
    target = remote_head(primary["repository"], target_ref)
    if target == old:
        print(f"{primary['name']} is already at {old}")
        return

    destination = PLUGIN_ROOT / primary["directory"]
    conflicts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cul-primary-") as temp:
        repo = Path(temp)
        run("git", "init", "--quiet", repo.as_posix())
        run("git", "remote", "add", "origin", primary["repository"], cwd=repo)
        run("git", "fetch", "--quiet", "origin", old, target, cwd=repo)
        files = git_files(repo, old) | git_files(repo, target)

        for relative in sorted(files):
            local_path = destination / relative
            base = git_bytes(repo, old, relative)
            remote = git_bytes(repo, target, relative)
            local = local_path.read_bytes() if local_path.is_file() else None
            if local == remote or remote == base:
                continue
            if local == base:
                if remote is None:
                    local_path.unlink(missing_ok=True)
                else:
                    write_file(local_path, remote, git_mode(repo, target, relative))
                continue
            if base is None and local is None and remote is not None:
                write_file(local_path, remote, git_mode(repo, target, relative))
                continue
            if base is None or local is None or remote is None or b"\0" in base + local + remote:
                conflicts.append(relative)
                continue
            merged, conflicted = merge_text(local, base, remote)
            write_file(local_path, merged, git_mode(repo, target, relative))
            if conflicted:
                conflicts.append(relative)

        tree = run("git", "rev-parse", f"{target}^{{tree}}", cwd=repo).stdout.decode().strip()

    if conflicts:
        joined = "\n".join(f"  - {path}" for path in conflicts)
        raise RuntimeError(f"upstream merge requires manual resolution:\n{joined}")
    update_pin(old, target, tree)
    print(f"prepared {primary['name']} update {old[:12]} -> {target[:12]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--ref")
    args = parser.parse_args()

    if args.command == "prepare":
        prepare(args.ref)
        return

    result = status()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for label, item in result.items():
            state = "update available" if item["outdated"] else "current"
            latest = item.get("latest", item.get("latest_path_change"))
            print(f"{label}: {item['name']}: {state} ({item['pinned'][:12]} -> {latest[:12]})")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"sync_upstream: {error}") from error
