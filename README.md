# Codex Computer Use on Linux

Improve how Codex sees, understands, and operates Linux desktops.

This repository is a fork of [OpenAI Codex](https://github.com/openai/codex)
and a home for Linux computer-use work. Its current focus is **same-session
computer use**: letting Codex inspect and operate applications that are already
running in your real desktop session while preserving their processes,
profiles, signed-in state, files, and open windows.

The project combines a native Linux Computer Use backend with optional
desktop-specific companion plugins. The backend provides broad Linux support;
the companions add stronger window capture, coordination, and background-input
behavior where a compositor exposes the required APIs.

## What works today

The required [`computer-use-linux`](./computer-use-linux/) plugin can:

- report whether screenshots, accessibility, window discovery, and input are
  ready on the current desktop;
- list running applications and compositor windows, including focus, bounds,
  app identity, and best-effort terminal process context;
- return size-bounded PNG or JPEG screenshots together with AT-SPI
  accessibility trees;
- click, drag, scroll, press shortcuts, and type text using semantic elements
  or coordinates;
- invoke native accessibility actions and edit supported text/value controls;
- target keyboard input at a verified window and report which accessibility
  element received focus; and
- run short, validated, fail-fast action batches against one exact window to
  reduce model round trips.

All supported desktops can use the core plugin on its own. Install a companion
only when you want the stronger same-session behavior described below.

## Supported backends

### Core Linux backend

The core backend selects the best window, capture, accessibility, and input
path exposed by the current session.

| Desktop/session | Window backend | Current support |
|---|---|---|
| GNOME Wayland or Xorg | GNOME Shell extension or Shell Introspect | Window discovery and focus, AT-SPI, GNOME/portal screenshots, and guarded input |
| KDE Plasma 5/6 | Temporary KWin D-Bus scripting | Window discovery and focus, AT-SPI, portal capture/input, and Plasma-aware text entry |
| Hyprland | `hyprctl` | Window discovery and focus plus the shared screenshot, accessibility, and input paths |
| Niri | Niri IPC | Window discovery and focus plus the shared screenshot, accessibility, and input paths |
| COSMIC Wayland | Bundled COSMIC toplevel helper | Window discovery and focus plus the shared screenshot, accessibility, and input paths |
| i3 | `i3-msg` and optional `xprop` | Window discovery and focus plus X11 accessibility, capture, and input paths |
| Generic Xorg/EWMH | `wmctrl` and `xprop` | Window discovery on desktops such as Xfce, Cinnamon, MATE, and LXQt/Openbox, with AT-SPI and X11 input |

Real behavior still depends on the session exposing its expected portal,
accessibility, compositor, and input services. Run the backend's `doctor`
command, or call its `doctor` tool from Codex, for the authoritative capability
report on your machine.

### Optional same-session companions

Companion plugins coordinate with the core backend but add their own
compositor-specific tools and safety boundaries.

| Companion | Declared target | What it adds | Input/interference model |
|---|---|---|---|
| [Hyprland](./contrib/hyprland-background-computer-use/) | Hyprland 0.55.4 | Exact capture of inactive windows, per-window agent claims, targeted shortcuts, native Wayland and XWayland pointer actions, and recoverable fallback workspaces | Window-local operations avoid the physical pointer; compatibility fallback uses an acknowledged global-seat lease |
| [GNOME](./contrib/gnome-same-session-computer-use/) | GNOME Shell 45 on Wayland or Xorg | Stable Shell window discovery, per-window claims, focused-window capture, and recoverable focus/workspace restoration | GNOME exposes one global seat, so coordinate and keyboard work is serialized and may visibly change focus |
| [KDE Plasma](./contrib/plasma-same-session-computer-use/) | Plasma 6/KWin Wayland | Stable KWin IDs, parallel exact compositor capture, cross-process window claims, and owner-bound recovery journals | Capture can remain in the background; input uses one acknowledged global-seat lease and is not claimed as targeted background input |
| [Generic X11](./contrib/x11-background-computer-use/) | EWMH Xorg desktops | Exact XComposite capture, per-window claims, best-effort no-focus shortcuts, and recoverable XTEST input | Capture can run per window; reliable input is serialized through the shared physical seat |

There is currently no separate same-session companion for Niri, COSMIC, or
i3; those desktops use the core backend capabilities in the first table.

The companions are experimental. Hyprland native extensions must match the
running compositor ABI, and the GNOME and Plasma companions intentionally make
narrower version claims than the generic core backend.

## Installation

### Requirements

- Linux running one of the core backends listed above
- a current [Codex CLI](https://developers.openai.com/codex/cli) release with
  `codex plugin` support
- Rust and `cargo`, because the source plugin is built on the target machine
- any system dependencies listed by the companion guide you choose

The bundled and repository-owned Computer Use plugins expose the same MCP
server name and must not be enabled together. Remove the bundled plugin first
if it is installed:

```shell
codex plugin remove computer-use@openai-bundled
```

### Install from the Git marketplace

This fetches only the marketplace metadata, backend, and companion plugins
instead of cloning the full Codex fork:

```shell
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse computer-use-linux \
  --sparse contrib/hyprland-background-computer-use \
  --sparse contrib/gnome-same-session-computer-use \
  --sparse contrib/plasma-same-session-computer-use \
  --sparse contrib/x11-background-computer-use
codex plugin add computer-use-linux@codex-computer-use-linux
```

The launcher performs a locked release build when Codex starts it. Cargo
reuses the external build cache when the source has not changed.

### Install from a local checkout

Use a local checkout when developing the backend or installing the GNOME Shell
extension:

```shell
git clone https://github.com/Gabriel-Kahen/codex-computer-use-linux.git
cd codex-computer-use-linux
codex plugin marketplace add "$PWD"
codex plugin add computer-use-linux@codex-computer-use-linux
```

Codex installs a source snapshot rather than a live link. Reinstall the plugin
after changing the backend or a companion.

### Add a companion plugin

Install the companion matching the current desktop session.

For Hyprland 0.55.4:

```shell
codex plugin add same-session-computer-use@codex-computer-use-linux
```

For GNOME Shell 45, install the Shell extension and companion:

```shell
./contrib/gnome-same-session-computer-use/bin/install-gnome-integration
codex plugin add gnome-same-session-computer-use@codex-computer-use-linux
```

For Plasma 6 Wayland:

```shell
codex plugin add plasma-same-session-computer-use@codex-computer-use-linux
```

For an EWMH Xorg desktop:

```shell
codex plugin add x11-background-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation so the tools and operating skill are
loaded, then verify the installed plugins:

```shell
codex plugin list
```

The [Hyprland](./contrib/hyprland-background-computer-use/README.md),
[GNOME](./contrib/gnome-same-session-computer-use/README.md),
[Plasma](./contrib/plasma-same-session-computer-use/README.md), and
[X11](./contrib/x11-background-computer-use/README.md) guides contain detailed
system requirements, update and removal instructions, safety boundaries, and
troubleshooting.

## Safety and limitations

Linux display servers expose different capture and input capabilities. Exact
background capture does not necessarily imply background input: GNOME, Plasma,
and generic X11 expose a shared seat, while Hyprland provides additional
window-targeted paths.

When a companion must use shared focus or input, it requires explicit
acknowledgement and records enough compositor state to restore the original
window, workspace, focus, minimized/fullscreen state, and pointer where the
desktop permits. The companions refuse input in unsafe conditions such as a
locked session, active pointer constraints, or a physical button being held.

Computer Use can read private on-screen and accessibility content and can
trigger arbitrary actions in the targeted application. It is not intended to
bypass authentication surfaces, application security controls, or anti-cheat
systems. Codex should still ask before actions that submit, delete, send,
purchase, overwrite, or otherwise commit state.

Read the companion's safety section before enabling it:
[Hyprland](./contrib/hyprland-background-computer-use/README.md#safety-boundary),
[GNOME](./contrib/gnome-same-session-computer-use/README.md#safety-boundary),
[Plasma](./contrib/plasma-same-session-computer-use/README.md#safety), or
[X11](./contrib/x11-background-computer-use/README.md).

## Build and development

The repository contains three independently built layers:

| Component | Source | Purpose |
|---|---|---|
| Codex | `codex-rs/` | CLI, TUI, app server, agent loop, and plugin host |
| Linux Computer Use | `computer-use-linux/upstream/` | Linux screenshots, AT-SPI state, window discovery, input, and diagnostics |
| Codex Linux integration | `computer-use-linux/` | Plugin launch, identity, provenance, update policy, and optional Chrome host |

The Linux backend intentionally remains outside the cross-platform `codex-rs`
Cargo workspace. Building Codex does not build the backend, and building the
backend does not rebuild Codex.

### Prerequisites

Install a current Rust toolchain, `cargo`, `just`, and the system dependencies
for the desktop integration. Fetch the Codex workspace dependencies with:

```shell
just install
```

`just install` does not install system packages. Backend tests also use
`cargo-nextest`:

```shell
cargo install --locked cargo-nextest
```

### Build the Codex fork

```shell
cargo build --manifest-path codex-rs/Cargo.toml -p codex-cli --bin codex
./codex-rs/target/debug/codex --version
```

The binary is written to `codex-rs/target/debug/codex`. Use
`just build-for-release` for the Bazel-based release build.

### Build and inspect the Linux backend

```shell
just computer-use-build
just computer-use-run doctor
```

The backend build cache defaults to
`${CODEX_COMPUTER_USE_LINUX_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/codex-computer-use-linux/target}`
so plugin installation does not copy Cargo artifacts.

To install the backend built with this checkout's Codex CLI:

```shell
codex plugin remove computer-use@openai-bundled # only when installed
PATH="$PWD/codex-rs/target/debug:$PATH" just computer-use-install
```

Run that command again after backend changes, then start a new Codex task.

### Test the backend

```shell
just computer-use-test
just computer-use-validate
just computer-use-chrome-test
```

The Chrome native-messaging host is optional and is not started by the Computer
Use MCP plugin.

### Update the backend baseline

The generic engine is pinned to
[`agent-sh/computer-use-linux`](https://github.com/agent-sh/computer-use-linux).
[`ilysenko/codex-desktop-linux`](https://github.com/ilysenko/codex-desktop-linux)
is monitored as a secondary patch feed. Check both without changing files:

```shell
just computer-use-upstream-status
```

`just computer-use-upstream-prepare` performs a three-way merge of a newer
primary revision while retaining this repository's Linux patch set. Scheduled
automation can open a tested draft PR but never merges it automatically.

Exact revisions and retained patch sources are recorded in
[`computer-use-linux/UPSTREAM.toml`](./computer-use-linux/UPSTREAM.toml) and
explained in the [provenance policy](./computer-use-linux/UPSTREAM.md).

### Desktop application boundary

This repository does not build the graphical Codex Desktop frontend. A desktop
installation produced by `codex-desktop-linux` must launch the Codex binary
from this checkout to use a modified agent runtime. This repository owns the
Computer Use backend and companion plugins; `codex-desktop-linux` remains
responsible for Linux desktop UI packaging and launchers.

## Project direction

Background operation is the current technical focus, but the broader goal is
to improve the complete Codex computer-use experience on Linux: wider desktop
support, better application compatibility, stronger accessibility integration,
more reliable visual and semantic interaction, safer recovery, and less
disruption to the person using the computer.

The generic engine remains a provenance-pinned upstream layer. Codex packaging
and product-specific integration are maintained independently here. The MCP
plugin boundary keeps Linux-only dependencies out of the cross-platform Codex
workspace while leaving room to move controller policy, approvals, leases, and
tool registration into Codex later.

## Credits, upstreams, and licenses

This repository builds on and periodically pulls from:

- [OpenAI Codex](https://github.com/openai/codex), the upstream for the CLI,
  TUI, app server, agent runtime, and plugin system in `codex-rs/`;
- [agent-sh/computer-use-linux](https://github.com/agent-sh/computer-use-linux),
  originally created by Avi Fenesh and maintained with its contributors, the
  primary upstream for `computer-use-linux/upstream/`;
- [ilysenko/codex-desktop-linux](https://github.com/ilysenko/codex-desktop-linux),
  the secondary feed for selected Linux fixes and the Chrome native host, with
  lineage from
  [avifenesh/codex-desktop-linux](https://github.com/avifenesh/codex-desktop-linux);
  and
- [Gabriel-Kahen/hyprland-codex-background-computer-use](https://github.com/Gabriel-Kahen/hyprland-codex-background-computer-use),
  the source repository for the Hyprland companion now maintained here.

Refer to the [official Codex documentation](https://developers.openai.com/codex)
for general Codex installation, authentication, IDE, app, and cloud usage. The
Codex fork and companion integrations are licensed under the
[Apache-2.0 License](LICENSE). The imported Linux backend retains its
[MIT license](computer-use-linux/LICENSE).
