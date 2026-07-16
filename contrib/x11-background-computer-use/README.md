# X11 background computer use for Codex

This experimental Codex plugin operates existing applications in the user's current local Xorg session. It targets EWMH window managers used by Xfce, Cinnamon, MATE, LXQt/Openbox, and legacy GNOME or KDE Xorg sessions. It can coexist with the Hyprland plugin because its plugin, MCP server, skill, state, cache, and launcher names are distinct.

## Capabilities

- Paginated EWMH discovery with XRes-authenticated PID ownership, XID, desktop, geometry, bounded title and WM_CLASS, and AT-SPI correlation hints.
- Exact unobscured capture of mapped windows through the compositor's XComposite named pixmap, without focus, desktop, stacking, or pointer changes.
- Best-effort no-focus shortcuts with an explicit unconfirmed-delivery result.
- Reliable keyboard, click, scroll, and drag input through an explicitly acknowledged XTEST focus/pointer lease.
- Cross-process, session-bound window claims keyed to Codex's host-supplied agent identity. Different windows can be captured or receive targeted shortcuts concurrently, while the same window is fenced to one agent for its observe-act-verify cycle.
- Expiring ownership, atomic private journals, per-window locks, and crash recovery that prevents another agent from ending or recovering a live owner's work.
- Crash-recoverable state journaling, held-input and lock-screen guards, button-release recovery, and restoration of desktop, focus, pointer, and target minimized state.

Generic X11 cannot provide both reliable input and zero interference. Many applications reject targeted XSendEvent input, so reliable actions use the shared physical seat and can briefly interrupt the user. The plugin reports this limitation rather than claiming Hyprland-equivalent targeted input.

## Requirements

- A local Xorg session with an EWMH-compatible window manager.
- `python3`, `xprop`, `wmctrl`, `xdotool`, `xinput`, logind, and XRes 1.2.
- For the capture and XRes ownership helper: a C compiler, `pkg-config`, and development packages for X11, XComposite, XRes, and libpng. Exact capture additionally requires an enabled X11 compositing manager and a direct-color visual, as used by normal modern Xorg desktops.
- The repository-owned Computer Use plugin for preferred AT-SPI semantic actions.

On Debian/Ubuntu:

```shell
sudo apt install python3 wmctrl xdotool xinput gcc pkg-config libx11-dev libxcomposite-dev libxres-dev libpng-dev
```

On Fedora:

```shell
sudo dnf install python3 wmctrl xdotool xinput gcc pkgconf-pkg-config libX11-devel libXcomposite-devel libXres-devel libpng-devel
```

On Arch Linux:

```shell
sudo pacman -S python wmctrl xdotool xorg-xinput gcc pkgconf libx11 libxcomposite libxres libpng
```

Enable your desktop's compositor if `session_status` reports `compositing_manager_active: false`. Minimized windows must be restored before exact capture. Capture is limited to 33,177,600 pixels (equivalent to 7680 × 4320), and PNGs larger than 5 MiB are rejected before transport.

## Install

```shell
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse computer-use-linux \
  --sparse contrib/x11-background-computer-use
codex plugin add computer-use-linux@codex-computer-use-linux
codex plugin add x11-background-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation. Call `session_status` before the first operation.

Capture on another virtual desktop is window-manager dependent: the window must remain mapped and its compositor must retain the named pixmap. Cancellation does not stop an input mutation already executing; wait for the tool result and then end or recover the lease.

## Parallel agents

Each agent should call `claim_session_window` before its first capture and retain the returned `claim_token` through the complete observe-act-verify cycle. Pass that token to window and input tools, then call `release_session_window` in finally-style cleanup. Calls from Codex are owned by the host-provided MCP `_meta.threadId`; a model cannot impersonate another owner with a tool argument.

Claims are atomic across MCP server processes, bound to the verified X server, window XID, authenticated PID, and process lifetime, and expire after 60 seconds by default (configurable from 5 to 300 seconds). Successful claimed operations renew the deadline. A crashed agent therefore cannot strand a window indefinitely, while a second live agent cannot steal, mutate, release, end, or recover the first agent's active work.

`list_window_claims` returns compact token-free records under a serialized byte cap and reports `truncated: true` if necessary. A claim bound to an input lease cannot be released or reacquired by another agent until that lease is ended or recovered, even if its ordinary claim deadline passes.

Parallelism follows the facilities X11 actually exposes:

| Operation | Concurrency |
| --- | --- |
| Window discovery | Fully concurrent |
| Exact XComposite capture | Concurrent across distinct claimed windows |
| Targeted XSendEvent shortcut | Concurrent across distinct claimed windows; delivery remains unconfirmed |
| Reliable XTEST key/pointer input | One serialized global-seat lane for the whole X11 session |

The global lane is intentional: stock X11 has only one reliable keyboard focus and physical pointer. Agents can keep reasoning, reading files, claiming other windows, and capturing other windows while a peer owns that lane, but reliable XTEST mutations cannot safely run at the same instant.

## Update and remove

Update by reinstalling from the marketplace:

```shell
codex plugin remove x11-background-computer-use@codex-computer-use-linux
codex plugin add x11-background-computer-use@codex-computer-use-linux
```

Remove cached binaries and unfinished state only after calling `recover_input_lease` and releasing live window claims:

```shell
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/codex-x11-background-computer-use"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/x11-same-session-computer-use"
```

No root daemon, compositor patch, or alternate desktop is installed.
