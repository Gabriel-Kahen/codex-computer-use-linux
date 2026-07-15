---
name: x11-same-session-computer-use
description: Operate applications in the user's current local EWMH Xorg login while preserving their processes, profiles, files, and signed-in state. Use for exact background capture or guarded control of existing apps on Xfce, Cinnamon, MATE, LXQt/Openbox, or legacy GNOME/KDE Xorg desktops.
---

# X11 Same-session Computer Use

Operate the real logged-in Xorg session. Never substitute a nested desktop, alternate `HOME`, duplicate profile, or isolated D-Bus session.

## Workflow

1. Call `session_status`, then `list_session_windows`. Stop if the session is not local Xorg/EWMH or the target is not proven same-UID.
2. Reuse the existing window. Its XID is stable only until that client window is destroyed; refresh the list before each operation batch.
3. Capture mapped, non-minimized windows with `capture_session_window`. XComposite capture does not focus, raise, move, or uncover the window.
4. Prefer semantic operations from the separate `computer-use@openai-bundled` plugin. Correlate AT-SPI applications using the returned PID, title, and WM_CLASS; never hardcode bus names or object paths.
5. Use `send_window_shortcut` only for low-risk discrete shortcuts when best-effort delivery is acceptable. Its XSendEvent result is not proof that the client accepted the event.
6. When reliable keyboard or coordinate input is necessary, obtain the user's explicit interference acknowledgment in the current task and call `begin_input_lease`. Use only `lease_*` tools until `end_input_lease` restores state.
7. Always end a lease in finally-style cleanup. If a broker call is interrupted, call `recover_input_lease` before doing anything else.
8. Capture again to verify the result.

## Safety boundary

- X11 has one shared keyboard focus and pointer. There is no generally reliable, non-interfering background input mechanism for modern toolkits.
- An input lease may switch desktop, unminimize and focus the target, and briefly move the physical pointer. It journals and restores the previous desktop, focus, pointer, and target minimized state.
- The broker refuses mutation while the login is locked, while a physical key/button is held, or when held input cannot be inspected.
- Do not begin a lease merely because a tool argument can acknowledge it. The user must have approved the interference boundary in the current task.
- Cancellation does not interrupt a mutation already running in the broker. Wait for its result, then end or recover the lease.
- Stop before destructive or irreversible actions unless the user explicitly authorized them.

See [architecture.md](references/architecture.md) for backend guarantees and limitations.
