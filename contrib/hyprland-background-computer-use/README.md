# Hyprland Codex Background Computer Use

Background computer control for Codex in the user's existing Hyprland session.

This project lets an automation agent inspect and operate the applications that are already running in the user's real desktop login. It preserves the same processes, profiles, signed-in sessions, files, and open windows instead of launching applications in a VM, nested compositor, or alternate home directory.

## Capabilities

- Enumerate live Hyprland windows and their workspaces.
- Claim windows for individual Codex tasks with expiring, cross-process fencing tokens.
- Run independent agents against different native Wayland windows concurrently.
- Capture an exact window on an inactive workspace without focusing or moving it.
- Send address-targeted keyboard shortcuts.
- Click, scroll, and drag inside background native Wayland windows without moving the physical cursor.
- Click, scroll, and drag inside background XWayland windows through XWayland's internal XTEST pointer.
- Work alongside the `computer-use-linux@codex-computer-use-linux` plugin for AT-SPI semantic controls, text editing, and global-input fallback.
- Fall back to a temporary headless output for focus-dependent applications.
- Fullscreen a fallback-only window over the temporary screen when necessary, then restore its original fullscreen mode, workspace, focus, and cursor.
- Recover compositor state after an interrupted fallback lease.

## Design

| Operation | Backend | Normal physical interference |
|---|---|---|
| Window discovery | `hyprctl clients -j` | None |
| Claim/list/release | Private atomic broker state | None |
| Background capture | `grim -T <stableId>` | None |
| Semantic UI actions | AT-SPI | None |
| Targeted shortcuts | Hyprland address dispatcher | None |
| Native Wayland pointer actions | Hyprland target-surface extension | None observed |
| XWayland pointer actions | XTEST internal pointer with restoration | None observed |
| Compatibility fallback | Temporary headless output | Brief input contention is possible |

The broker identifies each caller from the host-only `tools/call.params._meta.threadId`; tool arguments cannot override that identity. A window claim has a 60-second default lease (configurable from 5 to 300 seconds), an opaque `claim_token`, and an absolute `expires_at`. Reclaiming from the same task renews the lease without rotating its live token. Another task cannot capture or mutate the claimed window through this broker, even if it omits the token. Supplying the token adds explicit fencing, and a stale or wrong-window token is rejected.

Claim state is atomically replaced, mode `0600`, and scoped to the real user's Wayland display and Hyprland instance. Cross-process file locks make same-window claim and mutation races deterministic. Different native Wayland windows use separate locks and can progress concurrently. Same-window operations remain serialized. XWayland, global-seat, and coordinate-fallback operations use one global lane because those paths share compositor or XTEST state. The generic Computer Use server takes that same global lane before its window lock for focus changes, physical-seat input, and screenshots that raise their target, and consumes the same claim state and window locks for capture, focus, AT-SPI, portal, pointer, keyboard, text, and combined action-and-observe operations: pass the claim result's `owner_thread_id` and `claim_token` to those tools. Pass `raise_window=false` to keep exact capture non-focusing.

The native extension sends a complete event transaction directly to the selected Wayland surface, then restores pointer focus before the next compositor event. It never moves Hyprland's physical pointer. XWayland actions snapshot and restore XWayland's separate internal pointer.

The fallback reuses the same application process. It does not create another profile or login. The target may be fullscreened on the temporary output, and all recorded compositor state is restored afterward.

Window discovery is paginated and bounds compositor-provided text only when returning it over MCP, preserving full internal titles for reliable matching. PNG captures must pass bounded structural and pixel-stream validation before base64 encoding, and captures larger than 5 MiB are rejected so responses remain below Codex's stdio transport limit. Image results omit `structuredContent` so Codex receives the actual screenshot content blocks.

`get_session_window_capture` is read-only and returns its PNG inline. To persist a capture, use the separately destructive `save_session_window_capture` tool with an absolute `save_path`; it atomically replaces the destination only after a valid capture succeeds. The original `capture_session_window` tool remains as a deprecated compatibility route with its optional `save_path` behavior and destructive annotation.

## Parallel-agent workflow

For a prompt that can be split across windows, the coordinating agent should enumerate the current windows and give each worker a distinct target. Every worker then:

1. Calls `claim_session_window` before its first capture or action.
2. Passes the returned `claim_token` to `get_session_window_capture` or `save_session_window_capture`, targeted pointer/shortcut tools, and `begin_coordinate_lease`. Passes both `owner_thread_id` and `claim_token` to the generic Computer Use capture and mutation tools.
3. Lets claimed broker operations renew automatically, and calls `claim_session_window` before `expires_at` during longer external semantic work. A renewal from the same task keeps the token stable.
4. Recaptures immediately before coordinate selection and after each mutation.
5. Ends any coordinate lease and calls `release_session_window` in cleanup, including after failures.

`list_window_claims` reports bounded pages of current owners and expiry times without exposing fencing tokens. Active claims are capped at 128 records per session, while each response also has a serialized-byte cap and a continuation cursor. Expired claims are removed lazily and may be acquired by another task. Never copy a claim token to a different task: ownership is checked against the host-provided task identity as well as the token.

Claims preserve legacy unclaimed flows when no active claim conflicts. They become enforceable as soon as a claim exists or a caller supplies a token. This lets older single-agent clients continue to operate while parallel-aware agents get strict exclusion.

## Repository layout

- `hyprland/` — ABI-checked native Wayland target-pointer extension.
- `src/same_session_computer_use/` — MCP broker and transactional fallback manager.
- `skills/same-session-computer-use/` — Codex operating policy and architecture notes.
- `.codex-plugin/` and `.mcp.json` — Codex plugin metadata.
- `docs/plan.md` — implementation plan and supported boundary.

## Requirements

The implementation is Hyprland-specific and experimental. It was developed and accepted against Hyprland 0.55.4. Other releases are not currently supported. Native extensions must be rebuilt for the exact running Hyprland ABI.

Normal targeted pointer and shortcut actions snapshot the physical focus,
workspace, and cursor before and after dispatch. They return success only when
those observations match; a mismatch is reported as an error with the observed
changes instead of being attributed to the backend. The snapshots cannot
distinguish backend interference from concurrent physical user input, so normal
targeted actions never restore a potentially stale pre-action snapshot.

Runtime and build dependencies include:

- Hyprland and its development headers
- a C++23 compiler and `make`
- `pkg-config`
- `grim`
- `xdotool` with XTEST support
- Python 3.10 or newer
- an enabled AT-SPI session for semantic accessibility actions
- a Codex CLI release with `codex plugin` support
- `computer-use-linux@codex-computer-use-linux`, which provides the AT-SPI and global-input Computer Use tools that this plugin's skill coordinates with

The broker builds and loads the native extension on demand. Builds are cached outside the installed plugin under `${XDG_CACHE_HOME:-$HOME/.cache}/same-session-computer-use/` and keyed by the plugin source and running Hyprland version. Before using an already-loaded extension, the broker verifies its plugin version, source digest, and build/runtime Hyprland ABI. A stale or mismatched extension is rejected with instructions to unload it instead of being used silently.

`session_status` reports that identity under `versions`. It also reports semantic actions separately under `semantic_actions`: the companion knows that `computer-use-linux` is the provider and that its calls are not claim-enforced, but leaves availability unknown because the provider is a separate MCP process.

## Install

Add this Git repository as a marketplace, then install its Computer Use plugin and the Hyprland companion:

```bash
codex plugin marketplace add Gabriel-Kahen/codex-computer-use-linux --ref main \
  --sparse .agents/plugins \
  --sparse computer-use-linux \
  --sparse contrib/hyprland-background-computer-use
codex plugin add computer-use-linux@codex-computer-use-linux
codex plugin add same-session-computer-use@codex-computer-use-linux
```

Start a new Codex task after installation. The repository-owned `computer-use-linux@codex-computer-use-linux` plugin owns AT-SPI semantic actions and focus-dependent global input. This plugin adds task-owned window claims, exact Hyprland window capture, address-targeted shortcuts, native Wayland pointer targeting, XWayland pointer targeting, and the transactional headless-output lease. The generic plugin reads the same claim state and takes the same per-window locks across capture and mutation paths.

Check that Codex sees both plugins:

```bash
codex plugin list
```

## Update

Refresh the Git marketplace snapshot and reinstall the plugin, then start a new Codex task. If the older native extension is already loaded, unload it before updating or restart Hyprland afterward.

```bash
codex plugin marketplace upgrade codex-computer-use-linux
codex plugin add same-session-computer-use@codex-computer-use-linux
```

## Uninstall

Unload the native Hyprland extension before removing the plugin. The broker stores ABI-specific builds in its external cache, so try each cached library and remove that cache after the loaded one is released:

```bash
PLUGIN_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/same-session-computer-use/hyprland"
while IFS= read -r plugin; do
  hyprctl plugin unload "$plugin" >/dev/null 2>&1 || true
done < <(find "$PLUGIN_CACHE" -name same-session-target-pointer.so -type f 2>/dev/null)
codex plugin remove same-session-computer-use@codex-computer-use-linux
codex plugin marketplace remove codex-computer-use-linux
rm -rf "$PLUGIN_CACHE"
```

The repository-owned Computer Use plugin is shared infrastructure. Keep it installed if you use it elsewhere; otherwise remove it separately with `codex plugin remove computer-use-linux@codex-computer-use-linux`.

## Build the native extension

```bash
make -C hyprland clean all
hyprctl plugin load "$(realpath hyprland/same-session-target-pointer.so)"
hyprctl plugin list
```

The generated shared object is intentionally excluded from Git. Build it on the target machine so it matches that machine's Hyprland ABI.

## Test the native path

The `hyprland-native-e2e` workflow boots the supported Hyprland release in a NixOS VM with a virtual GPU and real input devices. It builds and loads the extension, captures a background GTK window, injects a click and shortcut into that window, and verifies that a foreground sentinel keeps its focus, workspace, and pointer state.

On a Hyprland development machine, run the same smoke test nested inside the current session:

```bash
PYTHONPATH=src python tests/native_e2e.py
```

## Run the MCP broker

```bash
./bin/same-session-computer-use-mcp
```

The included Codex plugin manifest registers this broker. Its skill coordinates these Hyprland-specific tools with the separate repository-owned Computer Use plugin's accessibility-first controls.

## Safety boundary

Hyprland still has one physical compositor seat. Normal window-local actions bypass that seat, but the compatibility fallback temporarily focuses the leased application and may contend with physical input. It therefore requires explicit acknowledgement and records the owning task, expiring ownership, claim, display, and Hyprland instance before acting. A foreign task cannot recover live work, but may restore an orphan after both owner and claim expiry. While a fallback is active, other XWayland/global-seat work is rejected; unrelated native Wayland windows can continue through their per-window lanes.

The extension refuses input while the session is locked, a physical button is held, pointer constraints are active, or drag-and-drop is in progress. It is not intended to bypass authentication surfaces, anti-cheat systems, or application security controls.
