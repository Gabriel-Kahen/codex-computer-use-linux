# Linux Computer Use for Codex

This directory is the source-owned Linux Computer Use plugin for this Codex
fork. It keeps the generic Linux engine, Codex packaging, and Codex-only browser
integration in explicit layers:

| Path | Ownership | Purpose |
|---|---|---|
| `upstream/` | Based on `agent-sh/computer-use-linux` | AT-SPI, screenshots, input, diagnostics, and compositor window backends |
| `bin/` and plugin manifests | This repository | Codex identity, launch, installation, and environment compatibility |
| `codex-integration/chrome-host/` | Selectively synchronized from `codex-desktop-linux` | Linux Chrome native messaging and bounded app-server runtime |

The engine supports GNOME, Plasma 5/6, Hyprland, Niri, COSMIC, i3, and generic
X11/EWMH window discovery. It prefers semantic AT-SPI actions, validates modern
`ydotool` before using it, and falls back through the available desktop capture
and input paths.

## Build and test

From the repository root:

```shell
just computer-use-build
just computer-use-run doctor
just computer-use-test
just computer-use-validate
```

The launcher builds the upstream crate into
`${CODEX_COMPUTER_USE_LINUX_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/codex-computer-use-linux/target}`.
Keeping the target outside the plugin source prevents plugin installation from
copying Cargo artifacts.

The launcher compiles the generic extension with the Codex GNOME UUID and DBus
identity through the upstream-supported `CUL_*` build variables. It also maps
legacy `CODEX_COMPUTER_USE_*` runtime variables to their generic equivalents.
No Codex string patch is required in the upstream engine.

The isolated Chrome host is optional and is not started by the MCP plugin:

```shell
just computer-use-chrome-test
```

## Install as a Codex plugin

Add this checkout as a local marketplace and install the source-owned package:

```shell
codex plugin marketplace add "$(pwd)"
codex plugin add computer-use-linux@codex-computer-use-linux
```

Do not enable it alongside `computer-use@openai-bundled`; both expose the MCP
server name `computer-use`. `just computer-use-install` builds and installs the
repository version. Reinstall after source changes and start a new Codex task so
the refreshed MCP tools are loaded.

## Upstream maintenance

[`UPSTREAM.toml`](UPSTREAM.toml) pins the exact primary and secondary source
revisions and records the local patch lineage. Check drift with:

```shell
just computer-use-upstream-status
```

Prepare a primary upstream update locally with:

```shell
just computer-use-upstream-prepare
```

The updater performs a file-level three-way merge using the pinned revision as
the base, so local Niri, Plasma, `ydotool`, and GNOME extension work is retained
or surfaced as an explicit conflict. It never commits or merges changes.

A scheduled GitHub workflow runs the same updater and, after format, Clippy,
test, and MCP safety checks, opens or refreshes a **draft** PR. It cannot merge
that PR. Changes in `codex-desktop-linux/computer-use-linux` are monitored as a
secondary patch feed and are ported selectively.

See [UPSTREAM.md](UPSTREAM.md) for the ownership and contribution policy.
