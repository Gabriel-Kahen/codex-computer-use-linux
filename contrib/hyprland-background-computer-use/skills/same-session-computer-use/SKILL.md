---
name: same-session-computer-use
description: Operate native applications in the user's real current Hyprland login while preserving their existing processes, profiles, signed-in sessions, open files, and application state. Use when the user asks Codex to view, screenshot, or control an already-open Linux app; to behave like macOS Computer Use; to work in the background without creating an isolated desktop; or to avoid disturbing the physical pointer, focus, and workspace.
---

# Hyprland Codex Background Computer Use

Operate the real logged-in session. Never substitute a VM, nested desktop, alternate `HOME`, fresh browser profile, duplicate application profile, or isolated D-Bus session.

## Workflow

1. Call `session_status` and `list_window_claims`, then call `list_session_windows`. Follow `next_cursor` through every bounded page until it is null so windows after the first page are not missed.
2. Reuse an existing matching window. Preserve its process, profile, login, open documents, workspace, and fullscreen state.
3. Call `claim_session_window` for that exact window. Keep its `claim_token` private to this task, record the returned `owner_thread_id`, pass the token to every broker capture/action, and renew before `expires_at` if work continues.
4. Capture inline with the read-only `get_session_window_capture` and the claim token. Use `save_session_window_capture` only when the user needs a PNG written to an absolute path. Neither operation focuses, moves, or raises the window. Do not use the deprecated `capture_session_window` compatibility tool in new workflows.
5. Inspect and act with the separate `computer-use-linux@codex-computer-use-linux` plugin's accessibility tools. Pass the claim's `owner_thread_id` and `claim_token` to its capture and mutation tools; the generic server shares the broker's per-window lock across those operations. Refresh app state immediately before choosing an element. If those tools are absent, stop and ask the user to install that companion plugin.
6. Prefer semantic AT-SPI operations in this order:
   - `perform_action` for buttons, links, menu items, and other actionable controls.
   - `set_value` or editable-text operations for text fields and sliders.
   - `send_window_shortcut` for discrete keys or shortcuts that Hyprland can deliver to a window address without focus.
7. For coordinate-only UI, use `targeted_pointer_click`, `targeted_pointer_scroll`, or `targeted_pointer_drag` with the claim token. These route directly to the selected Wayland surface or XWayland window and do not move the physical cursor or focus the app.
8. Capture the exact window again to verify the result, then release the window in `finally`-style cleanup.

## Parallel tasks

For one prompt with independent work in multiple windows, fan out one worker per window. Each worker must use its own host-provided task identity and claim only its assigned window. Different native Wayland windows can capture and mutate concurrently through this broker; actions on one window remain serialized. The generic Computer Use process shares those per-window locks for capture and mutation, and its physical-seat input and focusing screenshots share the broker's global lane. XWayland, physical-seat, and fallback operations may wait or fail while that lane is reserved.

Never share a `claim_token` between workers. A claim defaults to 60 seconds; claimed broker operations renew it, and the owner can explicitly renew from 5 to 300 seconds with `claim_session_window`. Same-owner renewal keeps the token stable. If a token expires, stop acting, reacquire the window, recapture it, and use the newly returned token. A foreign active claim is authoritative even when an action omits `claim_token`.

## Non-interference rules

- Do not use coordinate clicks, pointer moves, drags, global typing, or focus-changing keyboard injection when an accessibility or targeted-key route exists.
- Do not move a real window to a headless output merely to capture it; exact capture works on inactive workspaces.
- Do not launch another instance of an app when a usable existing instance is present.
- Treat window addresses and capture IDs as ephemeral. Refresh them before each operation batch.
- Do not capture, inspect, or mutate a window claimed by another task.
- Release claims only after the worker has ended its coordinate lease and finished all verification.
- Never hardcode AT-SPI bus names or object paths. Discover the current accessibility tree and match controls by role, name, and supported action.
- Stop if a requested action would overwrite unsaved work, close an app, sign out, or otherwise cause data loss without the user's explicit authorization.

## Targeted pointer rules

The same-session broker provides window-local pointer injection without moving Hyprland's physical cursor:

1. Refresh `list_session_windows` immediately before acting, follow `next_cursor` until it is null, and use the current address and claim token.
2. Use coordinates from the latest exact `get_session_window_capture` image. Convert screenshot pixels with the returned `coordinate_space.pixel_to_window_scale` before calling a pointer tool.
3. Keep every coordinate inside the returned window dimensions.
4. Prefer `targeted_pointer_click` over the headless lease fallback.
5. Verify the result with another exact capture.

Native Wayland events are delivered atomically by a version-matched Hyprland plugin. XWayland events are delivered through XWayland's internal XTEST pointer, which is distinct from Hyprland's physical cursor position.

## Headless output

A temporary Hyprland headless output is now an emergency compatibility fallback only. Use it when a client rejects both semantic accessibility actions and targeted pointer injection. Follow [architecture.md](references/architecture.md), obtain explicit interference acknowledgment from the user in the current task, and never treat the tool argument alone as proof of consent. Pass the window's claim token to `begin_coordinate_lease`. Leave `fullscreen_if_needed` enabled unless the task specifically requires the window's original fallback geometry. The broker fullscreens the target over the temporary screen and records its owning task, claim, display, Hyprland instance, and previous state. Only that task may capture, end, or recover the live lease; another task may recover it after ownership and claim expiry if the owner crashes. When using the separate Computer Use plugin for global fallback input, translate screenshot-local coordinates with the `coordinate_space` origin and scale returned by `capture_coordinate_desktop`. Always restore the lease before releasing the window claim, including after failures.
