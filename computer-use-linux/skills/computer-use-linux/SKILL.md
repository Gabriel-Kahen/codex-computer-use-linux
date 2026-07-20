---
name: computer-use-linux
description: Operate applications in the user's current Linux desktop with the computer-use-linux MCP tools. Use for observing, screenshotting, targeting, or controlling existing Linux GUI apps, and for coordinating those shared tools with installed Hyprland, GNOME, Plasma, or X11 same-session companion skills.
---

# Linux Computer Use

Operate the user's real desktop session. Reuse existing application processes, profiles, signed-in sessions, windows, and open files. Do not substitute a nested desktop, alternate home directory, fresh profile, or duplicate app instance.

## Route desktop coordination first

1. Use `doctor` when readiness or the active desktop backend is unknown.
2. When the matching companion skill is available, apply it before using shared observation or input tools:
   - Hyprland: `$same-session-computer-use:same-session-computer-use`
   - GNOME: `$gnome-same-session-computer-use:gnome-same-session-computer-use`
   - Plasma Wayland: `$plasma-same-session-computer-use:plasma-same-session-computer-use`
   - EWMH Xorg: `$x11-background-computer-use:x11-same-session-computer-use`
3. Treat companion window claims and focus/input leases as authoritative. The shared MCP server cannot mechanically enforce a companion's claims, so never use it to inspect or mutate a window claimed by another task.
4. Keep claim and lease tokens private to their owning task. Follow the companion's validation, renewal, recovery, and cleanup sequence exactly.
5. For Niri, COSMIC, and i3, use the shared plugin directly and serialize desktop mutations because no companion claim broker is provided.

## Observe and target

1. Begin every turn that uses Computer Use with `get_app_state`. Use `observation_mode: "adaptive"` for continued work on the same target.
2. Preserve the returned identifiers separately:
   - Echo `checkpoint_id` as `base_checkpoint_id` to request an adaptive visual delta.
   - Pass `observation_id` to element-targeted `click` and `scroll`, `perform_action`, and `set_value` calls derived from that accessibility snapshot.
3. Call `list_windows` or `focused_window` before targeted keyboard input. Prefer an exact `window_id`; otherwise use enough process, application, title, class, or terminal metadata to identify one window uniquely.
4. Treat window IDs, accessibility objects, coordinates, and observations as ephemeral. Refresh them after navigation, window recreation, or any result reporting expiry or a target mismatch.
5. Use screenshot coordinate metadata when coordinates are unavoidable. Account for scale and coordinate origin; never assume returned image pixels equal desktop coordinates.

## Act from safest to broadest

1. Prefer `perform_action` for buttons, links, menus, toggles, and other controls with a matching AT-SPI action.
2. Prefer `set_value` for editable fields, sliders, and other settable controls.
3. Use targeted `type_text` or `press_key` only after exact-window focus can be verified. Treat a missing or non-editable focused-element warning as failed delivery.
4. Use element-targeted pointer actions before raw coordinates. Use raw coordinates only when the application exposes no usable semantic control.
5. Use `run_action_batch` only for a short sequence against one exact `window_id`. Use `run_action_batch_and_observe` when post-action state is needed. Batches are fail-fast, not transactional, and may contain at most one leading click.
6. After every mutation, verify the result with `get_app_state`, an exact companion capture, `focused_window`, or application-specific readback.

## Protect the shared seat and user state

- Prefer semantic operations and compositor-native targeted actions over focus, keyboard, or pointer leases.
- Before a companion focus/input lease, obtain explicit user authorization for visible interference in the current task. A boolean tool argument is not evidence of consent.
- Do not use the shared keyboard or pointer while the user is physically interacting with the desktop.
- Never leave a claim or lease active while waiting for user input. End or recover the lease, then release the claim in finally-style cleanup.
- Stop on lock screens, authentication prompts, compositor modals, target ambiguity, focus-verification failure, stale observations, or lost claims. Do not silently retarget or fall back to global input.
- Obtain explicit authorization immediately before actions that submit, send, purchase, delete, overwrite, close unsaved work, or otherwise commit external state.
- Treat screenshots and accessibility trees as potentially sensitive user data. Capture only the intended app or region and do not expose unrelated desktop content.
