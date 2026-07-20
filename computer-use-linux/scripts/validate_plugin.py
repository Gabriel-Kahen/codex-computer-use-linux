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
