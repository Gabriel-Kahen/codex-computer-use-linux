# GNOME/Mutter Same-session Computer Use

This experimental Codex plugin operates applications already running in the user's real GNOME Shell session. Its current declared target is GNOME 45 on Wayland and Xorg through a user-installed Shell extension; it does not require a root daemon or patched distribution. Later GNOME releases are not declared until they receive compositor-session runtime testing.

## Honest capability boundary

| Operation | Implementation | Interference |
|---|---|---|
| Window discovery | `Meta.Window` through the Shell extension | None |
| Stable IDs | `Meta.Window.get_stable_sequence()` | None; IDs expire when Shell/window restarts |
| Agent coordination | Expiring, per-window claims keyed by Codex thread ID | Broker tools fence claimed windows; different windows proceed concurrently |
| Semantic actions | Separate bundled Computer Use AT-SPI plugin | Normally none; agents honor claims by policy because AT-SPI runs outside this broker |
| Exact capture | GNOME Shell focused-window screenshot | Requires target to already be focused or leased |
| Pointer/keyboard | Mutter `Clutter.VirtualInputDevice` | Uses the global seat; pointer is restored after each pointer transaction |
| Recovery | Shell-owned capability plus atomic, session-scoped state journals | Verifies workspace, focus, pointer, and minimized state before clearing the journal |

Stock Mutter does **not** expose arbitrary surface-targeted input or exact capture of an inactive window. This plugin therefore does not claim Hyprland-equivalent background input. Coordinate and keyboard operations require an explicitly acknowledged focus lease. The lease can visibly switch workspace/focus and can briefly contend with physical input.

The extension rejects input while the session is locked, the overview or a Shell modal is open, or Mutter reports a compositor grab. Mutter does not expose a reliable extension API for every physically held button, so the broker cannot prove that the hardware seat is idle.

Consent is enforced at the MCP broker/tool boundary. GNOME Shell separately enforces the lease protocol with a Shell-generated unguessable capability bound to the broker's unique D-Bus caller. For a claimed window, Shell also fences recovery until that claim's deadline; renewal remains restricted to the original D-Bus caller. A restarted broker may recover after the previous bus owner disappears, or after the claim expires, and only with the private journaled capability. This does not provide stronger isolation from arbitrary malicious code already running as the same Unix user.

## Requirements

- GNOME Shell 45 with Mutter, on Wayland or Xorg
- PyGObject (`python3-gobject`) for one persistent, caller-bound D-Bus connection
- `gdbus`, `gnome-extensions`, and Python 3.10+
- GNOME Shell's screenshot D-Bus service, or `gnome-screenshot` as a fallback
- `computer-use@openai-bundled` for AT-SPI semantic controls

## Install

Install and enable the local Shell integration first:

```sh
./contrib/gnome-same-session-computer-use/bin/install-gnome-integration
```

On Wayland, log out and back in if GNOME does not load the extension immediately. Then install the plugins:

```sh
codex plugin add computer-use@openai-bundled
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse contrib/gnome-same-session-computer-use
codex plugin add gnome-same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation. Call `session_status` before doing work.

## Workflow

1. Discover targets with `list_session_windows` and inspect `list_window_claims`, following each result's `next_cursor` for additional bounded pages.
2. Each parallel agent calls `claim_session_window`. Claims on distinct stable window IDs proceed concurrently; a same-window race has exactly one winner. The default claim is 60 seconds and `lease_seconds` accepts 5 through 300. Reclaiming a live claim as the same thread refreshes it; reacquiring after expiry rotates the token.
3. Pass `claim_token` to every claimed broker action. Prefer the companion plugin's AT-SPI actions, but treat the claim as a cooperative policy for those external actions: the separate AT-SPI process cannot be mechanically fenced by this broker.
4. For exact visual or coordinate work, ask the user to acknowledge interference and call `begin_focus_lease`. GNOME has one global seat, so focus leases, captures, pointer actions, and shortcuts remain one serialized lane even while independent semantic work runs concurrently.
5. Capture or operate the leased window. Always call `end_focus_lease`, including after failures, then `release_session_window`.
6. Use `recover_focus_lease` after an interruption. A journaled focus-lease target remains reserved even if its claim expires, so end or recover the focus lease before reacquiring that window.

Capture metadata reports screenshot pixel dimensions, logical window-local dimensions, and `pixel_to_window_scale`. Convert a screenshot point before pointer input: `window_x = screenshot_x * pixel_to_window_scale.x`, and likewise for y.

Private journals and locks live under `${XDG_STATE_HOME:-$HOME/.local/state}/gnome-same-session-computer-use/sessions/<session-id>/`, where the opaque ID binds them to the Unix user, session bus, and display. A matching v0.1 global focus journal is adopted once under a migration lock; state from another Shell session is left untouched. Writes are atomic and files/directories are mode `0600`/`0700`. Up to 128 claims may be live, and claim/window list pages are bounded and omit claim tokens. Per-window locks allow different-window broker work to proceed concurrently; one separate cross-process input lock serializes Mutter's global seat.

Recovery retains its journal if an actionable postcondition fails; inspect the returned mismatch and retry after resolving it. If a journaled window has closed, recovery reports `restored: false`, identifies it in `missing_windows`, and clears the now-unactionable journal when `recovery_complete` is true. `recovery_outcome_unknown: true` means the same Shell instance no longer holds an active lease after a lost recovery reply, so there is no remaining cleanup to retry but full restoration cannot be proven.

## Remove

```sh
codex plugin remove gnome-same-session-computer-use@codex-computer-use-linux
./contrib/gnome-same-session-computer-use/bin/remove-gnome-integration
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/gnome-same-session-computer-use"
```

If a lease is active, run `recover_focus_lease` before removal.

## Development

Run the broker tests from this directory:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --test tests/test_lease_protocol.mjs tests/test_shell_transactions.mjs
```

The Python suite includes real multiprocess claim races, simultaneous different-window authorization, nonowner fencing, expiry, and dead-broker recovery. The Node suite executes the capability/caller/phase/deadline state machine and the same transaction orchestration imported by the Shell extension. Fault-injectable adapters cover Shell safety gates, activation and restoration postconditions, and virtual pointer/keyboard release cleanup.

The thin Clutter, Mutter, and GNOME Shell adapter also received rootless runtime validation in a disposable Fedora 39 GNOME Shell 45.10/Mutter 45.7 nested Wayland compositor on Xvfb. That run enabled the extension, exercised D-Bus status and discovery with two real Wayland windows, rejected lease creation while Overview was open, leased and unminimized a target, delivered an `F6` event through Mutter's virtual keyboard, captured the focused window as a PNG, and recovered from a fresh broker process. Recovery restored the original focus, workspace, pointer, and minimized state before clearing the journal. This does not validate a GDM/systemd-logind-managed login, a real locked or modal session, a full GNOME Xorg session, the native DRM/input backend, or contention with a physical input seat.

The Shell extension is intentionally small and uses only APIs already loaded into GNOME Shell. Validate it in a real compositor session before declaring another Shell major because GNOME extensions share the Shell process and do not have a stable cross-version ABI.
