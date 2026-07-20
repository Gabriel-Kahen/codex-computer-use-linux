# Codex Computer Use on Linux

Improve how Codex sees, understands, and operates Linux desktops.

This repository is a fork of [OpenAI Codex](https://github.com/openai/codex)
and a home for Linux computer-use work. Its main feature is **same-session
computer use**: Codex can inspect and operate applications that are already
running in your real desktop session while keeping their processes, profiles,
signed-in state, files, and open windows.

Same-session computer use is one feature implemented across several desktop
backends. Every backend uses the shared Linux engine for screenshots,
accessibility, and input, then adds the compositor-specific behavior available
on that desktop.

## Features

- **Use your existing apps.** Codex works with the windows and applications you
  already have open instead of starting a separate desktop or fresh profile.
- **See both pixels and UI structure.** It combines screenshots with Linux
  accessibility information, window bounds, focus, app identity, and
  best-effort terminal process context.
- **Interact naturally.** Codex can click, drag, scroll, press shortcuts, type
  text, invoke accessibility actions, and edit supported controls using either
  semantic elements or coordinates.
- **Target the right window.** Supported backends can discover compositor
  windows, focus or claim an exact window, and verify targeted keyboard input.
- **Work in the background where the desktop allows it.** Hyprland can send
  input directly to a window, while Plasma and X11 can capture exact windows in
  the background. When a desktop exposes only one keyboard and pointer, the
  backend coordinates access and restores the user's state afterward.
- **Coordinate parallel agents.** The Hyprland, GNOME, Plasma, and X11 backends
  let each agent reserve a window so agents can work on different apps without
  racing each other. Actions that use the shared keyboard or pointer stay
  serialized.
- **Recover safely.** When an operation must borrow focus, a workspace, or the
  pointer, the backend records the original state and restores it after the
  action or an interrupted session.
- **Reduce round trips.** Short action batches are fully validated, run in
  order, stop on the first failure, and stay bound to one exact window.
- **Explain what is available.** The `doctor` command and tool report which
  screenshot, accessibility, window, and input paths are ready on the current
  machine.

## Supported desktop backends

| Desktop backend | Same-session capabilities | Background and input behavior | Declared support |
|---|---|---|---|
| [Hyprland](./contrib/hyprland-background-computer-use/) | Window discovery, exact inactive-window capture, AT-SPI, per-window claims, targeted shortcuts, and native Wayland/XWayland pointer actions | Window-local operations avoid the physical pointer; a recoverable workspace/output lease handles apps that require real focus | Full integration targets Hyprland 0.55.4; native extensions must match the running ABI |
| [GNOME](./contrib/gnome-same-session-computer-use/) | Shell window discovery, screenshots, AT-SPI, per-window claims, and recoverable focus/workspace restoration | GNOME exposes one global seat, so exact capture and coordinate/keyboard work use serialized, acknowledged focus leases | Full integration targets GNOME Shell 45 on Wayland or Xorg; the shared engine can use GNOME Shell or portal paths on other releases |
| [KDE Plasma](./contrib/plasma-same-session-computer-use/) | Stable KWin window IDs, AT-SPI, parallel exact capture, cross-process claims, and owner-bound recovery | Capture can stay in the background; keyboard and pointer work use one acknowledged global-seat lease | Full integration targets Plasma 6/KWin Wayland; the shared KWin backend supports window discovery and focus on Plasma 5/6 |
| [Generic X11/EWMH](./contrib/x11-background-computer-use/) | EWMH discovery, AT-SPI, exact XComposite capture, per-window claims, best-effort no-focus shortcuts, and recoverable XTEST input | Capture can run per window; reliable input is serialized through the shared physical seat | Xorg desktops such as Xfce, Cinnamon, MATE, LXQt/Openbox, and legacy GNOME or KDE |
| Niri | Niri IPC window discovery and focus plus shared screenshots, AT-SPI, and input | Uses the session's shared capture and input paths; no exact per-window background integration yet | Requires `NIRI_SOCKET` and working `niri msg` access |
| COSMIC Wayland | COSMIC toplevel discovery and focus plus shared screenshots, AT-SPI, and input | Uses the session's shared capture and input paths | Uses the bundled `computer-use-linux-cosmic` helper |
| i3 | i3 IPC window discovery and focus, optional process hydration, AT-SPI, capture, and X11 input | Uses the X11 shared seat for reliable input | Requires `i3-msg`; `xprop` adds process details |

Real behavior depends on the portal, accessibility, compositor, and input
services exposed by the current session. Call `doctor` for the authoritative
capability report on your machine.

Some backends are delivered as an additional desktop-integration plugin because
their compositor-specific code has different build and runtime requirements.
They are still part of the repository's same-session computer-use feature.

## Installation

### Requirements

- Linux running one of the supported desktop backends listed above
- a current [Codex CLI](https://developers.openai.com/codex/cli) release with
  `codex plugin` support
- `curl`, Python 3, `tar`, and `sha256sum` when installing a prebuilt release bundle
- glibc 2.35 or newer for prebuilt bundles; older systems can build from source
- Rust and `cargo` only when installing or developing from source
- any system dependencies listed by the desktop backend guide you choose

The bundled and repository-owned Computer Use plugins expose the same MCP
server name and must not be enabled together. Remove the bundled plugin first
if it is installed:

```shell
codex plugin remove computer-use@openai-bundled
```

### Install a prebuilt bundle

Release bundles contain the Codex Computer Use marketplace, its plugin sources, and
checksum-pinned `computer-use-linux` and COSMIC helper binaries. Select the
archive for the current architecture, verify it before extraction, and install
the extracted marketplace:

```shell
(
  set -euo pipefail
  releases="https://github.com/Gabriel-Kahen/codex-computer-use-linux/releases"
  release_tag="$(
    python3 - <<'PY'
import json
import urllib.request

url = "https://api.github.com/repos/Gabriel-Kahen/codex-computer-use-linux/releases?per_page=100"
while url:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-computer-use-installer"})
    with urllib.request.urlopen(request) as response:
        releases = json.load(response)
        links = response.headers.get("Link", "")
    for release in releases:
        tag = release["tag_name"]
        if tag.startswith("computer-use-v") and not release["draft"] and not release["prerelease"]:
            print(tag)
            raise SystemExit
    url = next(
        (part[part.index("<") + 1 : part.index(">")] for part in links.split(",") if 'rel="next"' in part),
        "",
    )
raise SystemExit("No published Linux Computer Use bundle release was found")
PY
  )"
  version="${release_tag#computer-use-v}"
  case "$(uname -m)" in
    x86_64 | amd64) target=x86_64-unknown-linux-gnu ;;
    aarch64 | arm64) target=aarch64-unknown-linux-gnu ;;
    *) echo "No prebuilt Linux Computer Use bundle for $(uname -m)" >&2; exit 1 ;;
  esac
  archive="codex-computer-use-linux-$version-$target.tar.gz"
  base="$releases/download/$release_tag"
  bundle_tmp="$(mktemp -d)"
  trap 'rm -rf "$bundle_tmp"' EXIT
  curl -fL "$base/$archive" -o "$bundle_tmp/$archive"
  curl -fL "$base/$archive.sha256" -o "$bundle_tmp/$archive.sha256"
  (cd "$bundle_tmp" && sha256sum --check --strict "$archive.sha256")
  install_root="${XDG_DATA_HOME:-$HOME/.local/share}/codex-computer-use-linux/$version-$target"
  mkdir -p "$install_root"
  tar --no-same-owner -xzf "$bundle_tmp/$archive" -C "$install_root"
  codex plugin marketplace add "$install_root"
  codex plugin add computer-use-linux@codex-computer-use-linux
)
```

The launcher verifies both bundled executables every time it starts. A missing
or modified executable fails closed instead of compiling or running a fallback.
If replacing an existing source installation, first run
`codex plugin remove computer-use-linux@codex-computer-use-linux` and
`codex plugin marketplace remove codex-computer-use-linux` so the marketplace
name can be registered at its new local path.

### Install from the Git marketplace (source build)

This fetches only the marketplace metadata, shared engine, and desktop backend
plugins instead of cloning the full Codex fork:

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

This is the source installation path. The launcher performs a locked release
build when Codex starts it, and Cargo reuses the external build cache when the
source has not changed.

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
after changing the shared engine or a desktop integration.

Set `CODEX_COMPUTER_USE_LINUX_BUILD_FROM_SOURCE=1` to explicitly rebuild from
source when running an extracted prebuilt bundle.

### Install the desktop integration

The shared `computer-use-linux` plugin is always required. Hyprland, GNOME,
Plasma, and generic X11 add a second package containing their compositor-specific
same-session integration. Niri, COSMIC, and i3 support is already contained in
the shared plugin.

For Hyprland 0.55.4:

```shell
codex plugin add same-session-computer-use@codex-computer-use-linux
```

For GNOME Shell 45, install the Shell extension and desktop integration:

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

When a backend must use shared focus or input, it requires explicit
acknowledgement and records enough compositor state to restore the original
window, workspace, focus, minimized/fullscreen state, and pointer where the
desktop permits. The backends refuse input in unsafe conditions such as a
locked session, active pointer constraints, or a physical button being held.

Computer Use can read private on-screen and accessibility content and can
trigger arbitrary actions in the targeted application. It is not intended to
bypass authentication surfaces, application security controls, or anti-cheat
systems. Codex should still ask before actions that submit, delete, send,
purchase, overwrite, or otherwise commit state.

Read the desktop backend's safety section before enabling it:
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
Computer Use backend integrations; `codex-desktop-linux` remains
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
  the source repository for the Hyprland integration now maintained here.

Refer to the [official Codex documentation](https://developers.openai.com/codex)
for general Codex installation, authentication, IDE, app, and cloud usage. The
Codex fork and desktop integrations are licensed under the
[Apache-2.0 License](LICENSE). The imported Linux backend retains its
[MIT license](computer-use-linux/LICENSE).
