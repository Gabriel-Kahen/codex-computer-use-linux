---
name: gnome-same-session-computer-use
description: Operate applications in the user's real current GNOME/Mutter session while preserving their processes, profiles, signed-in state, and open files. Use for existing GNOME desktop apps when the user permits an explicit, recoverable focus lease for coordinate or keyboard input.
---

# GNOME Same-session Computer Use

Operate the real logged-in GNOME session. Never replace it with a VM, nested desktop, alternate `HOME`, new browser profile, or duplicate app instance.

## Workflow

1. Call `session_status`, then `list_session_windows`.
2. Reuse the existing matching window.
3. Prefer semantic AT-SPI actions from `computer-use@openai-bundled`: actions first, then editable text/value operations.
4. Exact capture of an already focused window is safe. An inactive window requires a focus lease.
5. Before `begin_focus_lease`, obtain explicit interference acknowledgment from the user in the current task. A literal tool argument is not evidence of consent.
6. While leased, use coordinates from the latest capture. Multiply screenshot pixels by the returned `pixel_to_window_scale` to obtain logical window-local coordinates, and keep them inside the returned dimensions.
7. Always call `end_focus_lease` in cleanup. If the broker or prior task was interrupted, call `recover_focus_lease` before doing anything else.

## Non-interference rules

- GNOME has one global input seat. Never describe leased keyboard or pointer injection as background or non-interfering.
- Prefer AT-SPI over a focus lease, and prefer a discrete shortcut over coordinates.
- Do not begin a lease while the user is physically interacting with the desktop.
- Do not operate lock screens, authentication prompts, Shell modals, or destructive dialogs.
- Refresh the window list before every operation batch; stable sequence IDs expire when a window or GNOME Shell restarts.
- Stop if the target closes or loses focus during a lease. Restore rather than silently retargeting another window.
- Never leave a lease active while waiting for user input.
- If restoration reports a postcondition mismatch, keep the journal and retry recovery after resolving the mismatch.

Pointer transactions briefly move Mutter's global virtual pointer to the target and restore the prior position immediately. The workspace and keyboard focus remain leased until cleanup. Capture itself does not introduce an additional focus change.
