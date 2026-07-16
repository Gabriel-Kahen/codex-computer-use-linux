# KDE Plasma Codex Background Computer Use

Parallel same-session window capture and recoverable input coordination for Codex agents in the user's existing KDE Plasma Wayland login.

This is an experimental Plasma 6/KWin Wayland backend. It reuses the user's real application processes, profiles, files, and signed-in sessions. It does not start a nested desktop, require a root daemon, or patch KWin.

## Capability boundary

| Operation | Parallelism | Backend | User-visible interference |
|---|---|---|---|
| Window discovery and stable IDs | Parallel reads | KWin scripting via `kdotool` | None |
| Window ownership | Different windows may be claimed concurrently; one live owner per window | Cross-process claim files keyed by KWin UUID | None |
| Exact window capture | Different windows capture concurrently | `org.kde.KWin.ScreenShot2.CaptureWindow` plus per-window action locks | None expected; verified around every capture |
| Semantic actions | Different claimed windows may be handled concurrently | Separate Computer Use plugin over AT-SPI | Usually none; claim enforcement is caller policy across plugins |
| Arbitrary keyboard/pointer input | One global-seat focus lease at a time | Separate Computer Use plugin after owner-bound advisory validation | Focus, desktop, and pointer changes may be visible |
| Crash recovery | Any agent after owner exit or expiry | Persisted claim/focus journals plus KWin scripting | Restoration itself may be visible |

KWin's internal window UUID is used as the stable identifier for the life of a window. Exact capture is compositor-side and can capture a selected window from another virtual desktop. The helper never silently substitutes a full-desktop or active-window screenshot. A per-window lock fences each capture without serializing captures of other windows.

## Parallel-agent workflow

Codex supplies the calling task's identity as host-only MCP `_meta.threadId`; it is never accepted as a model-controlled tool argument. Before parallel work, each worker calls `claim_session_window` for its KWin UUID. The default claim lasts 60 seconds and `lease_seconds` may be 5 through 300. Renew by calling the tool again with the existing `claim_token`; an active claim always requires its token, even from the same task. `list_window_claims` reports compact, paginated, serialized-size-bounded coordination records without tokens, and `release_session_window` accepts only the secret `claim_token`.

An active claim rejects capture or focus setup by another task, and the owner must supply its token to `capture_plasma_window` or `begin_plasma_focus_lease`. Claims are persisted across MCP server processes, atomically replaced, limited to 128 live records, and immutably bound to the window UUID, owner task, claim token, user, boot, Wayland socket/session, and KWin D-Bus owner. Expired claims and claims whose owning MCP process exited can normally be recovered. A per-window action lock prevents release, expiry takeover, or renewal from crossing an in-flight capture. PID start time prevents PID reuse from impersonating a crashed owner.

KWin does **not** expose a stable public API for injecting arbitrary keyboard or pointer events directly into an inactive window. Accordingly, targeted background input remains unavailable. When AT-SPI cannot complete an action, the owning agent may request an acknowledged focus/restoration lease. An explicit claim is bound into that lease; an unclaimed window receives an implicit claim and returns its token. The broker allows only one such global-seat lease, snapshots focus/desktop/target/pointer state before activation, and binds validation, end, and live recovery to the owner. The focus journal reserves its target claim—even past the claim's ordinary expiry or broker-process exit—and neither owner nor peer may release or take it over until restoration finishes. Another agent can recover the focus lease only after expiry, owner-process exit, or a KWin session change.

Enforcement applies only to operations routed through this broker: its claim/release tools, exact capture, focus journal, and focus-lease lane. The broker cannot authorize, scope, disable, serialize, or gate AT-SPI or keyboard/pointer input performed by the separate Computer Use plugin, and that plugin cannot consume claim or lease tokens. Call `validate_plasma_focus_lease` immediately before every companion global-input action and proceed only when `advisory_ready` is true. That external hop remains caller policy; the lease token and deadline cannot disable another tool. Pointer restoration also remains the companion plugin's responsibility.

## Requirements

- KDE Plasma 6 in a Wayland session
- KWin's scripting and screenshot plugins
- Python 3.10 or newer and KDE's `qdbus6` command
- readable Linux `/proc` process metadata for crash-safe claim ownership and PID-reuse detection
- [`kdotool`](https://github.com/jinliu/kdotool) 0.2.3 or newer
- a C++17 compiler, `pkg-config`, and Qt 6 Core/Gui/DBus development files
- an enabled AT-SPI session for semantic actions
- `computer-use-linux@codex-computer-use-linux` for AT-SPI and separately invoked global input

Install `kdotool` from your distribution when available, or with `cargo install kdotool`. Common build packages are `qt6-base-dev g++ pkg-config` on Debian/Ubuntu, `qt6-qtbase-devel gcc-c++ pkgconf-pkg-config` on Fedora, and `qt6-base base-devel pkgconf` on Arch.

The first capture compiles a small Qt D-Bus helper under `${XDG_CACHE_HOME:-$HOME/.cache}/plasma-same-session-computer-use/`. It also installs a `NoDisplay` desktop entry under `${XDG_DATA_HOME:-$HOME/.local/share}/applications/` declaring KWin's restricted screenshot interface. This is user-local and needs neither root nor a compositor restart. Some distributions may apply additional ScreenShot2 policy; in that case the tool returns KWin's authorization error and keeps exact capture disabled.

The PNG returned to Codex is capped at 5 MiB so its base64-encoded MCP response remains below Codex's stdio transport limit. An oversized capture fails explicitly and does not replace `save_path`.

## Install

```bash
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse computer-use-linux \
  --sparse contrib/plasma-same-session-computer-use
codex plugin add computer-use-linux@codex-computer-use-linux
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

No KWin script is installed persistently: `kdotool` loads short-lived scripts over KWin's D-Bus scripting interface. Keep the core Computer Use plugin if other integrations use it.

## Safety

The broker refuses capture, focus changes, and restoration unless the standard Plasma screen-lock service positively reports the session unlocked. A new focus lease also requires the original active window, target desktop, minimized state, pointer, owning thread, owning process identity, active window claim, and current KWin/session identity to be positively recorded. It verifies that KWin actually activated the target. It permits one restoration journal at a time, stores private directories as mode `0700` and atomically replaced records as `0600`, preserves the target's minimized state, and provides explicit recovery after interruption. If the session locks before recovery, the journal is retained for a later unlocked recovery attempt.

Claim and focus durations are recovery deadlines, not background timers or external-input controls. The backend retains its focus journal and target reservation on material restoration or verification failures. It verifies immutable claim/session bindings and refuses stale restoration state before taking any KWin action or deleting a newer journal. If a recorded window has closed or the recorded KWin session no longer exists, recovery reports that full restoration was impossible instead of claiming success and removes state only when it is no longer safely actionable. A live owner cannot have its claim or focus lease released or recovered by another task. The backend does not bypass authentication surfaces, application security controls, capture exclusion requested by an application, or the physical user's control of the shared seat.
