# Codex Computer Use on Linux

Improve how Codex sees, understands, and operates Linux desktops.

This repository is a fork of [OpenAI Codex](https://github.com/openai/codex) and a home for work that makes Codex computer use more capable, reliable, safe, and native-feeling on Linux. The vision is broader than any one desktop environment, application, or interaction backend.

The current focus is **same-session background computer use**: letting Codex inspect and operate applications that are already running in the user's real desktop session while preserving their processes, profiles, signed-in state, files, and open windows. Whenever possible, Codex can work without taking over the user's focus, cursor, or workspace. Background app control is the first major workstream, not the limit of the project.

## Current support

The implementation currently available on `main` is an experimental integration for **Hyprland 0.55.4**. It combines the bundled Codex Computer Use plugin with a Hyprland-specific companion plugin in [`contrib/hyprland-background-computer-use/`](./contrib/hyprland-background-computer-use/).

The integration can:

- discover windows in the current Hyprland session;
- capture a specific window, including one on an inactive workspace;
- use AT-SPI accessibility controls for semantic interaction;
- target keyboard and pointer input at native Wayland and XWayland windows without moving the physical cursor where supported; and
- use a recoverable temporary workspace and output when an application requires real focus.

This is not yet a general Linux backend. Experimental backends for [GNOME](https://github.com/Gabriel-Kahen/codex-computer-use-linux/pull/10), [generic X11 desktops](https://github.com/Gabriel-Kahen/codex-computer-use-linux/pull/11), and [Plasma/KWin](https://github.com/Gabriel-Kahen/codex-computer-use-linux/pull/12) are also in development.

## Installation

### Requirements

- Linux running Hyprland 0.55.4
- a current [Codex CLI](https://developers.openai.com/codex/cli) release with `codex plugin` support
- the build and runtime dependencies listed in the [Hyprland integration guide](./contrib/hyprland-background-computer-use/README.md#requirements)

### Install the plugins

First install the bundled Computer Use plugin, which provides accessibility and global-input tools:

```shell
codex plugin add computer-use@openai-bundled
```

Then add this repository as a Codex plugin marketplace and install the Hyprland companion:

```shell
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/hyprland-background-computer-use
codex plugin add same-session-computer-use@codex-computer-use-linux
```

For Plasma 6 on Wayland, use the same marketplace but install the Plasma companion instead:

```shell
codex plugin add computer-use@openai-bundled
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/plasma-same-session-computer-use
codex plugin add plasma-same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation so the tools and operating skill are loaded. Confirm that both plugins are available:

```shell
codex plugin list
```

See the [Hyprland integration guide](./contrib/hyprland-background-computer-use/README.md) or [Plasma integration guide](./contrib/plasma-same-session-computer-use/README.md) for detailed requirements, updates, removal, manual builds, safety boundaries, and troubleshooting.

## Safety and limitations

Linux compositors expose different capture and input capabilities, so behavior and physical-input interference vary by desktop environment. The Hyprland integration prefers window-local operations, but its compatibility fallback can temporarily contend with the physical keyboard and pointer.

The fallback requires explicit acknowledgement and records compositor state so it can restore the original window, workspace, focus, fullscreen mode, and cursor position. The integration refuses input in unsafe conditions such as a locked session, active pointer constraints, or a physical button being held. It is not intended to bypass authentication surfaces, application security controls, or anti-cheat systems.

Read the full [safety boundary](./contrib/hyprland-background-computer-use/README.md#safety-boundary) before using the experimental integration.

## Project direction

Background operation is the current technical focus, but the broader goal is to improve the complete Codex computer-use experience on Linux. That includes wider desktop support, better application compatibility, stronger accessibility integration, more reliable visual and semantic interaction, safer recovery, and less disruption to the person using the computer.

## Upstream and license

This fork tracks the [OpenAI Codex](https://github.com/openai/codex) project. Refer to the [official Codex documentation](https://developers.openai.com/codex) for general Codex installation, authentication, IDE, app, and cloud usage.

Licensed under the [Apache-2.0 License](LICENSE).
