# Codex Computer Use on Linux

Standalone Linux desktop plugins for **stock Codex**. Inspect and operate the
apps already running in your desktop session, preserving their windows,
profiles, and signed-in state.

This is an independent community project, not an OpenAI product. It retains a
modified `agent-sh/computer-use-linux` engine and desktop integrations; it does
**not** contain or require a fork of the Codex runtime.

[CI](https://github.com/Gabriel-Kahen/codex-computer-use-linux/actions/workflows/computer-use-ci.yml)
· [Upstream update status](https://github.com/Gabriel-Kahen/codex-computer-use-linux/actions/workflows/computer-use-upstream-sync.yml)
· [Releases](https://github.com/Gabriel-Kahen/codex-computer-use-linux/releases)
· [Migration from the old fork](MIGRATION.md)

## Plugin and engine

The **plugin** is the installable Codex package: tool connection, launcher, and
operating instructions. The **engine** is the Rust program that implements
screenshots, accessibility, and input. Install the shared plugin and the
companion for your desktop; the companion coordinates desktop-specific capture,
window ownership, and input recovery.

| Component | Location | Role |
|---|---|---|
| Shared Codex plugin | `computer-use-linux/` | Launch and expose the engine over MCP |
| Modified Linux engine | `computer-use-linux/upstream/` | Pixels, accessibility, input, window backends |
| Desktop companions | `contrib/` | Hyprland, GNOME, Plasma, and X11 session integration |
| Optional Chrome host | `computer-use-linux/codex-integration/chrome-host/` | Separately built native messaging integration |

The Linux engine retains our window coordination, exact capture, action batch,
and recovery changes. Upstream updates are merged into that code, not copied
over it. [Provenance](computer-use-linux/UPSTREAM.md).

## Supported desktop backends

The most important distinction is between seeing a window and controlling it:

- **Exact background capture** returns the pixels of one inactive window
  without focusing, moving, or uncovering it.
- **Semantic control** invokes an application's accessibility actions or edits
  an accessible value directly. It is not simulated keyboard or pointer input,
  and support depends on the application and control.
- **Targeted background input** sends simulated input to one inactive window
  while another window remains focused.
- A **focus lease** temporarily activates the target, uses the desktop's shared
  keyboard or pointer, and then restores the previous state. It is reliable but
  may be visible and can briefly interfere with the user.

The table describes capabilities provided by this repository, not separate
application APIs such as browser automation, D-Bus, or OBS WebSocket.

| Backend                                                    | Exact capture while inactive | Semantic UI/text changes  | Shortcuts while inactive | Pointer while inactive |
| ---------------------------------------------------------- | ---------------------------- | ------------------------- | ------------------------ | ---------------------- |
| [Hyprland](./contrib/hyprland-background-computer-use/)    | **Yes**                      | **Application-dependent** | **Yes**                  | **Yes**                |
| [GNOME](./contrib/gnome-same-session-computer-use/)        | **No**                       | **Application-dependent** | **No**                   | **No**                 |
| [KDE Plasma](./contrib/plasma-same-session-computer-use/)  | **Yes**                      | **Application-dependent** | **No**                   | **No**                 |
| [Generic X11/EWMH](./contrib/x11-background-computer-use/) | **Usually**                  | **Application-dependent** | **Best effort**          | **No reliable path**   |
| Niri                                                       | **Yes, with GStreamer**       | **Application-dependent** | **No dedicated path**    | **No dedicated path**  |
| COSMIC Wayland                                             | **Yes**                      | **Application-dependent** | **No dedicated path**    | **No dedicated path**  |
| i3                                                         | **No dedicated path**        | **Application-dependent** | **No dedicated path**    | **No dedicated path**  |

In practical terms:

- **Hyprland** provides the strongest background control. Codex can see a
  window, send it shortcuts, and use its pointer while the user keeps working
  in another window. General text editing still prefers AT-SPI, and applications
  that insist on real focus use a recoverable temporary-output lease. Native
  Wayland pointer targeting uses a Hyprland extension; XWayland uses its
  internal XTEST pointer.
- **GNOME** provides same-session automation, but not general inactive-window
  capture or simulated input. Codex can sometimes operate an inactive window
  through AT-SPI; otherwise an acknowledged focus/workspace lease must briefly
  activate that window and restore the user's state afterward.
- **Plasma** provides true background window capture, but not true background
  keyboard or pointer input. Seeing an inactive window does not mean Codex can
  type or click in it without focusing it. An acknowledged focus/desktop lease
  provides the reliable fallback and restores focus, desktop, and pointer.
- **X11** provides true background capture and unreliable no-focus shortcuts.
  Reliable typing and pointer actions still require focus because modern
  applications often reject targeted synthetic events. An acknowledged XTEST
  lease focuses the target and restores desktop, focus, pointer, and minimized
  state. Exact capture requires a mapped window; minimized windows must first
  be restored.
- **Niri** can capture one inactive window through its compositor ScreenCast
  service, keyed to the same stable window ID returned by IPC. The optional
  path needs GStreamer's PipeWire, base, and good plugins and fails closed if
  the target disappears or changes identity; it never substitutes the desktop.
  Niri's screencast block-out rules are honored and may return black pixels.
  Current Niri IPC exposes no minimized-window state, so no additional
  minimized-window guarantee is claimed. Input still uses the shared generic
  engine.
- **COSMIC** provides exact inactive-window capture through its compositor's
  foreign-toplevel image-copy protocols. The persistent helper revalidates the
  stable toplevel identity and rejects stale or minimized windows; it never
  substitutes a desktop crop. Keyboard and pointer input still use the shared
  seat and may require focus.
- **i3** provides window discovery, focus, accessibility, and shared input
  through the generic engine, without stronger per-window background control.
  Compatible i3/EWMH sessions may instead use the X11 companion.

### Declared targets

- **Hyprland:** the full integration targets Hyprland 0.55.4. Its native
  extension must be built for the exact running Hyprland ABI.
- **GNOME:** the full Shell integration targets GNOME Shell 45 on Wayland or
  Xorg. The shared engine can use more limited Shell or portal paths on other
  releases, but those releases do not inherit the full integration's support
  claim.
- **Plasma:** the full integration targets Plasma 6 on KWin Wayland. The shared
  engine also provides basic KWin window discovery and focus on Plasma 5 and 6.
- **Generic X11/EWMH:** intended for Xorg desktops such as Xfce, Cinnamon,
  MATE, LXQt/Openbox, and legacy GNOME or KDE sessions.
- **Niri:** requires `NIRI_SOCKET`; `niri msg` is used only when the direct IPC
  event-stream or action path is unavailable. Exact inactive capture requires
  Niri's ScreenCast service plus `gst-launch-1.0` with `pipewiresrc`,
  `videoconvert`, and `pngenc`.
- **COSMIC Wayland:** uses the bundled persistent `computer-use-linux-cosmic`
  helper and requires the compositor's version-1 foreign-toplevel image-capture
  source and image-copy-capture protocols for exact background capture.
- **i3:** requires `i3-msg`; `xprop` adds process details when available.

Real behavior depends on the portal, accessibility, compositor, and input
services exposed by the current session. Call `doctor` for the authoritative
capability report on your machine.

Some backends are delivered as an additional desktop-integration plugin because
their compositor-specific code has different build and runtime requirements.
They are still part of the repository's same-session computer-use feature.

## Installation

Use a released bundle for a reproducible installation. Git `main` is the
integration branch and can change between releases. See
[stock Codex compatibility](docs/stock-codex.md) for the tested client version
and the scope of automated checks.

Requirements: Linux, a Codex client with `codex plugin` support, Python 3.12+,
GitHub CLI (`gh`), and the selected desktop's system dependencies. Prebuilt
engine binaries support x86_64 and aarch64 with glibc 2.35 or newer. The Hyprland
native extension still builds locally against the exact compositor ABI.

### Download and verify a release

From a checkout of this repository:

```sh
python3 scripts/install_release.py --version 0.6.0
```

The helper verifies the release archive's SHA-256 and GitHub build provenance,
extracts it into a versioned data directory, and prints the registration
commands. It does not replace an installed plugin or load a compositor
extension. Reusing an existing destination is refused so rollback copies remain
unchanged. To download elsewhere, pass `--destination /absolute/new/path`.

Register the printed bundle path, then install:

```sh
codex plugin marketplace add /absolute/path/to/verified/bundle
codex plugin add computer-use-linux@codex-computer-use-linux
# Choose the companion for your desktop:
codex plugin add same-session-computer-use@codex-computer-use-linux          # Hyprland
# codex plugin add gnome-same-session-computer-use@codex-computer-use-linux # GNOME
# codex plugin add plasma-same-session-computer-use@codex-computer-use-linux # Plasma
# codex plugin add x11-background-computer-use@codex-computer-use-linux     # X11
```

Niri, COSMIC, and i3 use the shared plugin without an additional companion.
GNOME also needs its Shell extension; follow its backend guide. Start a **new
Codex task** after installation and ask it to run the engine's `doctor` and the
companion's status tool. Supported backends and actual available capabilities
are different: status reports are authoritative for the current session.

Do not enable this shared plugin alongside `computer-use@openai-bundled`:
both expose the MCP server name `computer-use`. For existing marketplace
registrations, follow [migration and rollback](MIGRATION.md) instead of adding
a second copy.

### Source installation

Developers can install a pinned source tag with Rust/Cargo available:

```sh
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref computer-use-v0.6.0
codex plugin add computer-use-linux@codex-computer-use-linux
codex plugin add same-session-computer-use@codex-computer-use-linux
```

The launcher uses a locked Cargo build and caches artifacts outside the plugin
source. No Codex build is involved. A Git marketplace or local source checkout
is a snapshot at installation time; reinstall after changing source.

## Updates and maintenance

- Weekly automation checks the engine upstream and prepares a three-way merge.
  Candidate changes must pass validation before a draft update PR is offered.
- Conflicts and failed checks remain failed and have a deduplicated maintenance
  issue with a link to the run. A green run must not hide a blocked update.
- Dependency and GitHub Actions update PRs are proposed separately. The upstream
  engine baseline remains recorded by exact revision.
- Release publishing is gated on backend/companion checks, stock Codex
  compatibility, and the isolated native Hyprland test. Both architectures are
  bundled with checksums and signed build provenance.
- Releases never silently replace installed versions. Upgrade deliberately,
  retain the previous bundle, and use the same registration procedure to roll back.
- A new Hyprland version is not supported merely because the plugin builds.
  Add a tested compositor version before claiming support; the native extension
  must match the running ABI.

See [maintenance policy](docs/maintenance.md) for release and update procedures.

## Development

Install Rust, Cargo, `just`, Python 3.12+, and desktop-specific build packages.
The CI workflows list the reproducible build dependencies.

```sh
just computer-use-build
just computer-use-run doctor
just check
just computer-use-upstream-status
```

`just check` runs local engine, packaging, companion, and optional Chrome-host
checks. It does not manipulate your active desktop. CI additionally runs native
X11/Plasma checks and Hyprland in an isolated virtual machine. Those checks do
not certify every application or every Linux compositor version.

## Safety and limitations

Accessibility support varies by application. Background capture does not imply
background input. Shared-seat fallback can briefly interfere with physical
input; the desktop companions coordinate access and restore recorded state.
Review the backend guide before enabling that fallback. No integration should
bypass authentication surfaces or application security controls.

Report bugs in this repository's Issues. For security reports, use
[SECURITY.md](SECURITY.md). Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md).

## Attribution and history

This project was extracted from the independently maintained
[Codex fork](https://github.com/Gabriel-Kahen/codex-computer-use-linux-legacy)
at `e8d4ff733c6caf04f16c47260c708000270050d7`. Earlier history remains there.
The engine originated with Avi Fenesh and
[agent-sh/computer-use-linux](https://github.com/agent-sh/computer-use-linux),
with selectively imported work from
[ilysenko/codex-desktop-linux](https://github.com/ilysenko/codex-desktop-linux).
The Hyprland companion originated in
[hyprland-codex-background-computer-use](https://github.com/Gabriel-Kahen/hyprland-codex-background-computer-use).

The engine retains its [MIT license](computer-use-linux/LICENSE); other code
retains its respective notices and the repository [Apache-2.0 license](LICENSE).
Codex and OpenAI names and marks belong to their respective owners.
