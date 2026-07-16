---
name: x11-same-session-computer-use
description: Operate applications in the user's current local EWMH Xorg login while preserving their processes, profiles, files, and signed-in state. Use for exact background capture or guarded control of existing apps on Xfce, Cinnamon, MATE, LXQt/Openbox, or legacy GNOME/KDE Xorg desktops.
---

# X11 Same-session Computer Use

Operate the real logged-in Xorg session. Never substitute a nested desktop, alternate `HOME`, duplicate profile, or isolated D-Bus session.

## Workflow

1. Call `session_status`, then `list_session_windows`. Stop if the session is not local Xorg/EWMH or the target is not proven same-UID.
2. Reuse the existing window. Its XID is stable only until that client window is destroyed; refresh the list before each operation batch.
3. Call `claim_session_window`, retain its `claim_token`, and pass the token to every capture or action through the complete observe-act-verify cycle. Distinct windows may be claimed by distinct agents concurrently. Never work around a foreign live claim.
4. Capture mapped, non-minimized windows with `capture_session_window`. XComposite capture does not focus, raise, move, or uncover the window.
5. Prefer semantic operations from the separate `computer-use@openai-bundled` plugin. Correlate AT-SPI applications using the returned PID, title, and WM_CLASS; never hardcode bus names or object paths.
6. Use `send_window_shortcut` only for low-risk discrete shortcuts when best-effort delivery is acceptable. Its XSendEvent result is not proof that the client accepted the event.
7. When reliable keyboard or coordinate input is necessary, obtain the user's explicit interference acknowledgment in the current task and call `begin_input_lease` with the claim token. Use only `lease_*` tools until `end_input_lease` restores state. This reliable XTEST lane is serialized across agents because X11 has one global seat.
8. Always end an input lease and release the window claim in finally-style cleanup. If a broker call is interrupted, the owning agent should call `recover_input_lease`; another agent must wait for the expiring ownership deadline.
9. Capture again before releasing the claim to verify the result.

## Safety boundary

- X11 has one shared keyboard focus and pointer. There is no generally reliable, non-interfering background input mechanism for modern toolkits.
- Claims and exact capture are per-window and cross-process. They enable parallel agents on distinct windows; they do not make the single XTEST seat parallel.
- An input lease may switch desktop, unminimize and focus the target, and briefly move the physical pointer. It journals and restores the previous desktop, focus, pointer, and target minimized state.
- The broker refuses mutation while the login is locked, while a physical key/button is held, or when held input cannot be inspected.
- Do not begin a lease merely because a tool argument can acknowledge it. The user must have approved the interference boundary in the current task.
- Cancellation does not interrupt a mutation already running in the broker. Wait for its result, then end or recover the lease.
- Stop before destructive or irreversible actions unless the user explicitly authorized them.

See [architecture.md](references/architecture.md) for backend guarantees and limitations.
