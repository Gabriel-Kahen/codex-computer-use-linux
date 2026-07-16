# Codex Computer Use on Linux

Improve how Codex sees, understands, and operates Linux desktops.

This repository is a fork of [OpenAI Codex](https://github.com/openai/codex) and a home for work that makes Codex computer use more capable, reliable, safe, and native-feeling on Linux. The vision is broader than any one desktop environment, application, or interaction backend.

The current focus is **same-session background computer use**: letting Codex inspect and operate applications that are already running in the user's real desktop session while preserving their processes, profiles, signed-in state, files, and open windows. Whenever possible, Codex can work without taking over the user's focus, cursor, or workspace. Background app control is the first major workstream, not the limit of the project.

## Current support

The implementations currently available on `main` are a repository-owned native Linux Computer Use backend plus experimental integrations for **Hyprland 0.55.4**, **Niri**, **GNOME Shell 45+**, **generic X11/EWMH desktops**, and **KDE Plasma 5/6**. Each desktop integration combines the core backend in [`computer-use-linux/`](./computer-use-linux/) with an optional compositor-specific companion plugin.

The Hyprland integration in [`contrib/hyprland-background-computer-use/`](./contrib/hyprland-background-computer-use/) can:

- discover windows in the current Hyprland session;
- capture a specific window, including one on an inactive workspace;
- use AT-SPI accessibility controls for semantic interaction;
- target keyboard and pointer input at native Wayland and XWayland windows without moving the physical cursor where supported; and
- use a recoverable temporary workspace and output when an application requires real focus.

The GNOME integration in [`contrib/gnome-same-session-computer-use/`](./contrib/gnome-same-session-computer-use/) discovers and captures windows through a GNOME Shell extension. Because GNOME exposes a global input seat, coordinate and keyboard operations require an explicitly acknowledged, journaled focus lease rather than claiming non-interfering background targeting.

The Plasma integration in [`contrib/plasma-same-session-computer-use/`](./contrib/plasma-same-session-computer-use/) adds stable KWin window discovery, cross-process per-window agent claims, parallel exact compositor-side capture, and owner-bound recoverable focus/restoration leases. It serializes its global-seat fallback and does not claim targeted background input because Plasma exposes one shared input seat.

The X11 integration in [`contrib/x11-background-computer-use/`](./contrib/x11-background-computer-use/) supports EWMH window discovery, XComposite capture, and an acknowledged interference lease for desktops running a real Xorg session.

The core backend is desktop-aware, while the same-session background-control
layer remains experimental and desktop-specific.

## Build from source

The source is split into independently built components:

| Component | Source | Purpose |
|---|---|---|
| Codex | `codex-rs/` | CLI, TUI, app server, agent loop, and plugin host |
| Linux Computer Use | `computer-use-linux/upstream/` | Linux-only screenshots, AT-SPI state, window discovery, and input |
| Codex Linux integration | `computer-use-linux/` | Plugin launch, identity, provenance, update policy, and an optional separately built Chrome host |

The Linux backend intentionally remains outside the cross-platform `codex-rs`
Cargo workspace. Building Codex does not build the backend, and building the
backend does not rebuild Codex.

### Prerequisites

Install a current Rust toolchain (including `cargo`), `just`, and the build and
runtime dependencies for your desktop integration. From the repository root,
verify the pinned toolchain and fetch the Codex workspace dependencies with:

```shell
just install
```

`just install` does not install system packages. The backend test recipe also
uses `cargo-nextest`; install it if necessary:

```shell
cargo install --locked cargo-nextest
```

### Build the Codex fork

From the repository root, run:

```shell
cargo build --manifest-path codex-rs/Cargo.toml -p codex-cli --bin codex
./codex-rs/target/debug/codex --version
```

The resulting CLI is `codex-rs/target/debug/codex`. Use
`just build-for-release` when you specifically need the repository's
Bazel-based release build.

### Build the Linux Computer Use backend

```shell
just computer-use-build
just computer-use-run doctor
```

The backend is built in
`${CODEX_COMPUTER_USE_LINUX_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/codex-computer-use-linux/target}`.
Keeping this target directory outside the plugin source prevents Codex from
copying gigabytes of Cargo artifacts when it installs the plugin.

### Install a development build

Make sure `computer-use@openai-bundled` is not enabled, then build and install
the local source snapshot with the Codex CLI from this checkout:

```shell
codex plugin remove computer-use@openai-bundled # only when currently installed
PATH="$PWD/codex-rs/target/debug:$PATH" just computer-use-install
```

The `PATH` prefix makes the recipe use the Codex CLI built from this checkout.
Without it, the recipe uses whichever compatible `codex` executable is already
on `PATH`. Run `just computer-use-install` again after backend edits because
the plugin manager installs a snapshot rather than referencing the working tree
directly. Start a new Codex task after refreshing the plugin.

### Test the backend

```shell
just computer-use-test
just computer-use-validate
```

This runs the standalone backend suite without adding its Linux-only
dependencies to the main Codex workspace.

### Update the backend baseline

The generic engine is pinned to
[`agent-sh/computer-use-linux`](https://github.com/agent-sh/computer-use-linux);
[`ilysenko/codex-desktop-linux`](https://github.com/ilysenko/codex-desktop-linux)
is monitored as a secondary patch feed. Check both without changing files:

```shell
just computer-use-upstream-status
```

`just computer-use-upstream-prepare` performs a three-way merge of a newer
primary revision while retaining the repository's Linux patch set. Scheduled
automation can only open a tested draft PR and never merges it automatically.

### Desktop application boundary

This repository does not build the graphical Codex Desktop frontend. A desktop
installation produced by `codex-desktop-linux` must be configured to launch the
Codex binary from this checkout if you want the UI to use your modified agent
runtime. The Computer Use backend and companion plugins are now developed and
built here; `codex-desktop-linux` remains responsible for the Linux desktop UI
packaging and launcher.

## Installation

### Requirements

- Linux running Hyprland, Niri, GNOME Shell 45+, a supported Xorg/EWMH desktop, COSMIC, i3, or KDE Plasma 5/6
- a current [Codex CLI](https://developers.openai.com/codex/cli) release with `codex plugin` support
- the build and runtime dependencies listed in the corresponding [Hyprland](./contrib/hyprland-background-computer-use/README.md#requirements), [GNOME](./contrib/gnome-same-session-computer-use/README.md#requirements), [X11](./contrib/x11-background-computer-use/README.md#requirements), or [Plasma](./contrib/plasma-same-session-computer-use/README.md#requirements) integration guide

The bundled and repository-owned Computer Use plugins expose the same MCP
server name and must not be enabled together. If `codex plugin list` shows
`computer-use@openai-bundled`, remove it before using either installation path:

```shell
codex plugin remove computer-use@openai-bundled
```

### Install from the Git marketplace

This fetches only the marketplace metadata, core backend, and desktop companion
plugins instead of cloning the entire Codex fork:

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

The plugin launcher invokes a locked release build on the target Linux machine
when Codex starts it, so this installation path also requires Rust and `cargo`.
Cargo reuses the external build cache when the source has not changed.

### Install from a local checkout

Use this path when developing the backend or when installing the GNOME Shell
companion, whose extension installer runs from the checkout:

```shell
git clone https://github.com/Gabriel-Kahen/codex-computer-use-linux.git
cd codex-computer-use-linux
codex plugin marketplace add "$PWD"
codex plugin add computer-use-linux@codex-computer-use-linux
```

For backend development, use the build and refresh workflow above. Codex
installs a source snapshot, not a live link, so reinstall after changing the
backend or a companion plugin.

### Install the desktop companion

The core backend can be used by itself. Install one matching companion when you
want its desktop-specific capture, targeting, or recovery behavior.

For Hyprland:

```shell
codex plugin add same-session-computer-use@codex-computer-use-linux
```

For GNOME, first install its Shell extension and then install its companion plugin:

```shell
./contrib/gnome-same-session-computer-use/bin/install-gnome-integration
codex plugin add gnome-same-session-computer-use@codex-computer-use-linux
```

For Plasma 5 or 6, install the Plasma companion:

```shell
codex plugin add plasma-same-session-computer-use@codex-computer-use-linux
```

For Xorg/EWMH:

```shell
codex plugin add x11-background-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation so the tools and operating skill are
loaded, then confirm the installed plugins:

```shell
codex plugin list
```

See the [Hyprland integration guide](./contrib/hyprland-background-computer-use/README.md), [GNOME integration guide](./contrib/gnome-same-session-computer-use/README.md), [X11 integration guide](./contrib/x11-background-computer-use/README.md), or [Plasma integration guide](./contrib/plasma-same-session-computer-use/README.md) for detailed requirements, updates, removal, manual builds, safety boundaries, and troubleshooting.

## Safety and limitations

Linux display servers and compositors expose different capture and input capabilities, so behavior and physical-input interference vary by desktop environment. The Hyprland integration prefers window-local operations, while its compatibility fallback and the X11 integration can temporarily contend with the physical keyboard and pointer. GNOME and Plasma provide exact background capture but use acknowledged focus/restoration leases before global input.

The fallback requires explicit acknowledgement and records compositor state so it can restore the original window, workspace, focus, fullscreen mode, and cursor position. The integration refuses input in unsafe conditions such as a locked session, active pointer constraints, or a physical button being held. It is not intended to bypass authentication surfaces, application security controls, or anti-cheat systems.

Read the full [Hyprland](./contrib/hyprland-background-computer-use/README.md#safety-boundary), [GNOME](./contrib/gnome-same-session-computer-use/README.md#safety-boundary), [X11](./contrib/x11-background-computer-use/README.md), or [Plasma](./contrib/plasma-same-session-computer-use/README.md#safety) safety boundary before using an experimental integration.

## Project direction

Background operation is the current technical focus, but the broader goal is to improve the complete Codex computer-use experience on Linux. That includes wider desktop support, better application compatibility, stronger accessibility integration, more reliable visual and semantic interaction, safer recovery, and less disruption to the person using the computer.

The complete Linux backend now lives in this repository. The generic engine is maintained as a provenance-pinned upstream layer; Codex packaging and product-specific integration remain independently editable here. It remains an MCP plugin boundary for the first integration stage so Linux-only dependencies do not enter the cross-platform Codex workspace. Future work can move controller policy, approvals, leases, and tool registration into Codex while reusing this backend implementation.

## Credits, upstreams, and licenses

This repository builds on and periodically pulls from the following projects:

- [OpenAI Codex](https://github.com/openai/codex) is the upstream for the CLI,
  TUI, app server, agent runtime, and plugin system in `codex-rs/`.
- [agent-sh/computer-use-linux](https://github.com/agent-sh/computer-use-linux),
  originally created by Avi Fenesh and now maintained with its contributors, is
  the primary upstream for the generic Linux desktop-control engine copied into
  `computer-use-linux/upstream/`.
- [ilysenko/codex-desktop-linux](https://github.com/ilysenko/codex-desktop-linux)
  is the secondary feed for selected Linux fixes and Codex-specific integration,
  including the Chrome native host. Its lineage comes from
  [avifenesh/codex-desktop-linux](https://github.com/avifenesh/codex-desktop-linux).
- [Gabriel-Kahen/hyprland-codex-background-computer-use](https://github.com/Gabriel-Kahen/hyprland-codex-background-computer-use)
  is the source repository for the Hyprland companion now maintained under
  `contrib/hyprland-background-computer-use/`.

Exact backend revisions and retained patch sources are recorded in
[`computer-use-linux/UPSTREAM.toml`](./computer-use-linux/UPSTREAM.toml) and
explained in the [provenance policy](./computer-use-linux/UPSTREAM.md).

Refer to the [official Codex documentation](https://developers.openai.com/codex)
for general Codex installation, authentication, IDE, app, and cloud usage. The
Codex fork and companion integrations are licensed under the
[Apache-2.0 License](LICENSE). The imported Linux backend retains its
[MIT license](computer-use-linux/LICENSE).
