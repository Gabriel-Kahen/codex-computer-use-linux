# KDE Plasma Codex Background Computer Use

Same-session window capture and recoverable input coordination for Codex in the user's existing KDE Plasma Wayland login.

This is an experimental Plasma 6/KWin Wayland backend. It reuses the user's real application processes, profiles, files, and signed-in sessions. It does not start a nested desktop, require a root daemon, or patch KWin.

## Capability boundary

| Operation | Backend | User-visible interference |
|---|---|---|
| Window discovery and stable IDs | KWin scripting via `kdotool` | None |
| Exact window capture | `org.kde.KWin.ScreenShot2.CaptureWindow` | None expected; verified around every capture |
| Semantic actions | separate Computer Use plugin over AT-SPI | Usually none |
| Arbitrary keyboard/pointer input | separate Computer Use plugin after advisory focus revalidation | Focus, desktop, and pointer changes may be visible |
| Crash recovery | persisted lease journal plus KWin scripting | Restoration itself may be visible |

KWin's internal window UUID is used as the stable identifier for the life of a window. Exact capture is compositor-side and can capture a selected window from another virtual desktop. The helper never silently substitutes a full-desktop or active-window screenshot.

KWin does **not** expose a stable public API for injecting arbitrary keyboard or pointer events directly into an inactive window. Accordingly, this backend reports targeted background input as unavailable. When AT-SPI cannot complete an action, the agent may request an acknowledged focus/restoration lease that snapshots the active window, virtual desktop, target desktop, and pointer; activates and verifies the target; and later restores KWin state. Pointer restoration is coordinated with the separate global-input plugin and is never claimed automatically.

The broker cannot authorize, scope, disable, or gate global input performed by another plugin. Its token only controls access to the restoration journal, and its deadline is only an advisory revalidation/recovery signal. Callers must invoke `validate_plasma_focus_lease` immediately before every companion global-input action and proceed only when `advisory_ready` is true; that rule is advisory policy outside broker enforcement.

## Requirements

- KDE Plasma 6 in a Wayland session
- KWin's scripting and screenshot plugins
- Python 3.10 or newer and KDE's `qdbus6` command
- [`kdotool`](https://github.com/jinliu/kdotool) 0.2.3 or newer
- a C++17 compiler, `pkg-config`, and Qt 6 Core/Gui/DBus development files
- an enabled AT-SPI session for semantic actions
- `computer-use@openai-bundled` for AT-SPI and separately invoked global input

Install `kdotool` from your distribution when available, or with `cargo install kdotool`. Common build packages are `qt6-base-dev g++ pkg-config` on Debian/Ubuntu, `qt6-qtbase-devel gcc-c++ pkgconf-pkg-config` on Fedora, and `qt6-base base-devel pkgconf` on Arch.

The first capture compiles a small Qt D-Bus helper under `${XDG_CACHE_HOME:-$HOME/.cache}/plasma-same-session-computer-use/`. It also installs a `NoDisplay` desktop entry under `${XDG_DATA_HOME:-$HOME/.local/share}/applications/` declaring KWin's restricted screenshot interface. This is user-local and needs neither root nor a compositor restart. Some distributions may apply additional ScreenShot2 policy; in that case the tool returns KWin's authorization error and keeps exact capture disabled.

The PNG returned to Codex is capped at 5 MiB so its base64-encoded MCP response remains below Codex's stdio transport limit. An oversized capture fails explicitly and does not replace `save_path`.

## Install

```bash
codex plugin add computer-use@openai-bundled
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/plasma-same-session-computer-use
codex plugin add plasma-same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task, then ask it to call `plasma_session_status`. `exact_capture_transport_available` means KWin and the helper prerequisites are present. `exact_background_window_capture` becomes true only after an authorized capture actually succeeds.

## Update

```bash
codex plugin marketplace upgrade codex-computer-use-linux
codex plugin add plasma-same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task after updating.

## Remove

Recover an unfinished lease before uninstalling whenever possible:

```bash
codex plugin remove plasma-same-session-computer-use@codex-computer-use-linux
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/plasma-same-session-computer-use"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/plasma-same-session-computer-use"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/plasma-same-session-capture.desktop"
```

No KWin script is installed persistently: `kdotool` loads short-lived scripts over KWin's D-Bus scripting interface. Keep the bundled Computer Use plugin if other integrations use it.

## Safety

The broker refuses capture, focus changes, and restoration unless the standard Plasma screen-lock service positively reports the session unlocked. A new focus lease also requires the original active window, target desktop, minimized state, and pointer to be positively recorded. It verifies that KWin actually activated the target. It permits one restoration journal at a time, stores its private state directory as mode `0700` and journals as `0600`, preserves the target's minimized state, and provides explicit recovery after interruption. If the session locks before recovery, the journal is retained for a later unlocked recovery attempt.

The duration is an advisory recovery deadline, not a background timer or input control: call `recover_plasma_focus_lease` after an expired or interrupted transaction. The backend retains its journal on material restoration or verification failures. If a recorded window has closed, recovery reports that full restoration was impossible instead of claiming success, then removes the no-longer-actionable journal after the remaining state is safely handled. The backend does not bypass authentication surfaces, application security controls, or capture exclusion requested by an application.
