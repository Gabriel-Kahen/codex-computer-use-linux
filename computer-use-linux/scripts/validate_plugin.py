#!/usr/bin/env python3
"""Validate the repository marketplace to Linux Computer Use launcher chain."""

import json
import os
from pathlib import Path
import tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_skill(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"Skill front matter is invalid: {path}"
    front_matter, separator, _body = text.removeprefix("---\n").partition("\n---\n")
    assert separator, f"Skill front matter is invalid: {path}"
    metadata = {}
    for line in front_matter.splitlines():
        key, separator, value = line.partition(":")
        assert separator, f"Skill front matter line is invalid: {line}"
        metadata[key.strip()] = value.strip()
    return metadata, text


def main() -> None:
    manifest = read_json(PLUGIN_ROOT / ".codex-plugin/plugin.json")
    assert PLUGIN_ROOT.name == manifest["name"]
    prebuilt_version = (PLUGIN_ROOT / "PREBUILT_VERSION").read_text(encoding="utf-8").strip()
    assert manifest["version"] == prebuilt_version

    mcp = read_json(PLUGIN_ROOT / manifest["mcpServers"])
    server = mcp["mcpServers"]["computer-use"]
    launcher = PLUGIN_ROOT / server["command"]
    assert launcher.is_file(), f"MCP launcher is missing: {launcher}"
    assert os.access(launcher, os.X_OK), f"MCP launcher is not executable: {launcher}"

    assert manifest["skills"] == "./skills/"
    skill_path = PLUGIN_ROOT / manifest["skills"] / "computer-use-linux/SKILL.md"
    skill_metadata, skill_text = read_skill(skill_path)
    assert (skill_path.parent / "agents/openai.yaml").is_file()
    assert skill_metadata["name"] == "computer-use-linux"
    assert skill_metadata["description"]
    for required_guidance in [
        "observation_id",
        "checkpoint_id",
        "window claims",
        "focus/input lease",
        "explicit authorization",
    ]:
        assert required_guidance in skill_text, (
            f"Linux Computer Use skill is missing required guidance: {required_guidance}"
        )

    marketplace = read_json(REPO_ROOT / ".agents/plugins/marketplace.json")
    entry = next(item for item in marketplace["plugins"] if item["name"] == manifest["name"])
    assert entry["source"] == {"source": "local", "path": "./computer-use-linux"}
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_INSTALL"

    with (PLUGIN_ROOT / "UPSTREAM.toml").open("rb") as handle:
        provenance = tomllib.load(handle)
    upstream = PLUGIN_ROOT / provenance["primary"]["directory"]
    assert (upstream / "Cargo.toml").is_file()
    assert (upstream / "src/main.rs").is_file()
    build_identity = {}
    for line in (PLUGIN_ROOT / "prebuilt-build.env").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        build_identity[key] = value
    assert build_identity == {
        "CUL_GNOME_EXTENSION_UUID": "codex-window-control@openai.com",
        "CUL_DBUS_SERVICE": "com.openai.Codex.WindowControl",
        "CUL_DBUS_OBJECT_PATH": "/com/openai/Codex/WindowControl",
    }
    print("Linux Computer Use plugin and provenance chain are valid")


if __name__ == "__main__":
    main()
