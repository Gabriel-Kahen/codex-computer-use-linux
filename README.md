# Codex Computer Use on Linux

Improve how Codex sees, understands, and operates Linux desktops.

This repository is a fork of [OpenAI Codex](https://github.com/openai/codex) and a home for work that makes Codex computer use more capable, reliable, safe, and native-feeling on Linux. The vision is broader than any one desktop environment, application, or interaction backend.

The current focus is **same-session background computer use**: letting Codex inspect and operate applications that are already running in the user's real desktop session while preserving their processes, profiles, signed-in state, files, and open windows. Whenever possible, Codex can work without taking over the user's focus, cursor, or workspace. Background app control is the first major workstream, not the limit of the project.

## Current support

The implementations currently available on `main` are experimental integrations for **Hyprland 0.55.4** and **GNOME Shell 45**. Each combines the bundled Codex Computer Use plugin with a desktop-specific companion plugin.

The integration can:

- discover windows in the current Hyprland session;
- capture a specific window, including one on an inactive workspace;
- use AT-SPI accessibility controls for semantic interaction;
- target keyboard and pointer input at native Wayland and XWayland windows without moving the physical cursor where supported; and
- use a recoverable temporary workspace and output when an application requires real focus.

The GNOME integration in [`contrib/gnome-same-session-computer-use/`](./contrib/gnome-same-session-computer-use/) discovers and captures windows through a GNOME Shell extension. Because GNOME exposes a global input seat, coordinate and keyboard operations require an explicitly acknowledged, journaled focus lease rather than claiming non-interfering background targeting.

This is not yet general Linux desktop support. Experimental backends for [generic X11 desktops](https://github.com/Gabriel-Kahen/codex-computer-use-linux/pull/11) and [Plasma/KWin](https://github.com/Gabriel-Kahen/codex-computer-use-linux/pull/12) are also in development.

## Installation

### Requirements

- Linux running Hyprland 0.55.4 or GNOME Shell 45
- a current [Codex CLI](https://developers.openai.com/codex/cli) release with `codex plugin` support
- the build and runtime dependencies listed in the [Hyprland](./contrib/hyprland-background-computer-use/README.md#requirements) or [GNOME](./contrib/gnome-same-session-computer-use/README.md#requirements) integration guide

### Install the plugins

First install the bundled Computer Use plugin, which provides accessibility and global-input tools:

```shell
codex plugin add computer-use@openai-bundled
```

Then add this repository as a Codex plugin marketplace and install the companion for your desktop.

For Hyprland:

```shell
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/hyprland-background-computer-use
codex plugin add same-session-computer-use@codex-computer-use-linux
```

For GNOME, first install its Shell extension and then install its companion plugin:

```shell
./contrib/gnome-same-session-computer-use/bin/install-gnome-integration
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/gnome-same-session-computer-use
codex plugin add gnome-same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation so the tools and operating skill are loaded. Confirm that both plugins are available:

```shell
codex plugin list
```

See the [Hyprland](./contrib/hyprland-background-computer-use/README.md) or [GNOME](./contrib/gnome-same-session-computer-use/README.md) integration guide for detailed requirements, updates, removal, and troubleshooting.

## Safety and limitations

Linux compositors expose different capture and input capabilities, so behavior and physical-input interference vary by desktop environment. The Hyprland integration prefers window-local operations, but its compatibility fallback can temporarily contend with the physical keyboard and pointer. GNOME coordinate and keyboard input always uses a global virtual seat and therefore requires a focus lease.

The fallback requires explicit acknowledgement and records compositor state so it can restore the original window, workspace, focus, fullscreen mode, and cursor position. The integration refuses input in unsafe conditions such as a locked session, active pointer constraints, or a physical button being held. It is not intended to bypass authentication surfaces, application security controls, or anti-cheat systems.

Read the relevant [Hyprland](./contrib/hyprland-background-computer-use/README.md#safety-boundary) or [GNOME](./contrib/gnome-same-session-computer-use/README.md#safety-boundary) safety boundary before using an experimental integration.

## Project direction

Background operation is the current technical focus, but the broader goal is to improve the complete Codex computer-use experience on Linux. That includes wider desktop support, better application compatibility, stronger accessibility integration, more reliable visual and semantic interaction, safer recovery, and less disruption to the person using the computer.

## Upstream and license

This fork tracks the [OpenAI Codex](https://github.com/openai/codex) project. Refer to the [official Codex documentation](https://developers.openai.com/codex) for general Codex installation, authentication, IDE, app, and cloud usage.

Licensed under the [Apache-2.0 License](LICENSE).
