---
name: gnome-same-session-computer-use
description: Operate applications in the user's real current GNOME/Mutter session, including parallel agents on separately claimed windows, while preserving their processes, profiles, signed-in state, and open files. Use for existing GNOME desktop apps when the user permits an explicit, recoverable focus lease for coordinate or keyboard input.
---

# GNOME Same-session Computer Use

Operate the real logged-in GNOME session. Never replace it with a VM, nested desktop, alternate `HOME`, new browser profile, or duplicate app instance.

## Workflow

1. Call `session_status`, `list_session_windows`, and `list_window_claims`; follow `next_cursor` for every additional bounded page.
2. Reuse the existing matching window and call `claim_session_window` before delegating work. Give each parallel agent a different stable window ID. Never have two agents race actions on one window.
3. Keep the returned `claim_token` private and pass it to every claimed broker action. Refresh a long-running claim before `expires_at`; a post-expiry reacquire returns a new token and is blocked while a focus-lease journal still reserves the window.
4. Prefer semantic AT-SPI actions from `computer-use-linux@codex-computer-use-linux`: actions first, then editable text/value operations. That companion runs outside this broker, so claims coordinate AT-SPI agents by policy and do not mechanically prevent a non-cooperating external action.
5. Capture a mapped claimed window with the read-only `get_session_window_capture`; when `window_actor_capture_protocol` is available, capture does not change focus or require a focus lease. A minimized window fails closed on that path. Use `save_session_window_capture` only when the user needs a mapped-window PNG written to an absolute path, and do not use the deprecated `capture_session_window` compatibility tool in new workflows. An older extension without window-actor capture still requires the target to be focused or leased.
6. When fresh minimized pixels are necessary, use `capture_minimized_session_window`; it briefly activates the target under a recovery lease and may show its workspace, but returns no pixels unless client damage is painted and minimized state, workspace, focus, and pointer are restored.
7. Before `capture_minimized_session_window`, `act_and_observe_window`, or `begin_focus_lease`, obtain explicit interference acknowledgment from the user in the current task. A literal tool argument is not evidence of consent. Expect another agent's active focus transaction or lease to make this lane temporarily unavailable; continue independent semantic work or retry after that agent restores it.
8. For one coordinate or shortcut action, prefer `act_and_observe_window`; it captures the result and restores the desktop before returning. Use coordinates from the latest capture, multiply screenshot pixels by `pixel_to_window_scale`, and keep them inside the returned dimensions.
9. Use `begin_focus_lease` only for a sequence that cannot fit one short transaction. Always call `end_focus_lease` before `release_session_window`.
10. If the broker or prior task was interrupted, call `recover_focus_lease` before starting another focus lease.

## Non-interference rules

- GNOME has one global input seat. Never describe leased keyboard or pointer injection as background or non-interfering.
- Window claims coordinate cooperative Codex agents; they are not a security boundary against arbitrary processes running as the same Unix user.
- A live claim is exclusive to its host-supplied Codex thread ID. Never copy a claim token to another agent or put a thread ID in tool arguments.
- Claims on different windows and cooperative AT-SPI work can overlap. Window-actor captures are serialized but do not consume the focus lease; focus, pointer, shortcut, and legacy focused-window capture transactions use the global input lane.
- Prefer AT-SPI over a focus lease, and prefer a discrete shortcut over coordinates.
- Do not begin a lease while the user is physically interacting with the desktop.
- Do not operate lock screens, authentication prompts, Shell modals, or destructive dialogs.
- Refresh the window list before every operation batch; stable sequence IDs expire when a window or GNOME Shell restarts.
- Stop if the target closes or loses focus during a lease. Restore rather than silently retargeting another window.
- Never leave a lease active while waiting for user input.
- If restoration reports `recovery_complete: false`, keep the journal and retry after resolving the mismatch. A closed journaled window can instead produce a complete partial recovery with `restored: false` and no retained journal. If `recovery_outcome_unknown` is true, report the uncertainty but do not retry cleanup.
- Never let a claim expire while its focus lease is active. Refresh first, or end the focus lease and release the claim.

Pointer transactions briefly move Mutter's global virtual pointer to the target and restore the prior position immediately. `act_and_observe_window` restores workspace and keyboard focus before returning; longer focus leases retain them until cleanup. Mapped capture introduces no focus change. Fresh minimized capture deliberately uses a short focus/workspace activation transaction to solicit a new client frame, then verifies restoration before returning.
