---
name: plasma-same-session-computer-use
description: Operate applications in the user's existing KDE Plasma Wayland session with exact KWin capture and acknowledged focus/restoration leases.
---

# Plasma same-session computer use

Use this companion with `computer-use@openai-bundled`. It adds KWin window identity, exact compositor capture, and state recovery; the bundled plugin owns AT-SPI and global input.

1. Call `plasma_session_status`. Do not claim a capability whose boolean is false.
2. Call `list_plasma_windows` and keep the stable KWin UUID. Re-resolve after a window closes or restarts.
3. Prefer AT-SPI semantic inspection and actions from Computer Use. They usually avoid focus changes.
4. Use `capture_plasma_window` for exact visual verification when `exact_background_window_capture` is true. If only `exact_capture_transport_available` is true, one first capture attempt is allowed to test KWin authorization; report any rejection and do not substitute a desktop screenshot. It captures the selected window even when another desktop is active when KWin authorizes ScreenShot2.
5. Only when AT-SPI cannot perform an action, call `begin_plasma_focus_lease` with `acknowledge_interference: true`. Tell the user that the shared focus, desktop, keyboard, and pointer may visibly change.
6. Immediately before every separate Computer Use global-input action, call `validate_plasma_focus_lease`. Proceed only when `advisory_ready` is true. This is caller policy: the broker cannot authorize, scope, disable, or gate the separate tool, and the token and deadline do not constrain external input.
7. Keep each action short. Never interact with lock screens, password prompts, authentication agents, anti-cheat software, or security boundaries.
8. If pointer input was used, read `pointer_before` and move the pointer back with Computer Use before ending the lease when possible. Then always call `end_plasma_focus_lease`, including after a failed action.
9. Call `recover_plasma_focus_lease` after interruption, an advisory deadline, or a stale journal.

KWin does not expose a stable public interface for arbitrary input directly to an inactive surface. Never describe companion input as background, invisible, isolated, lease-scoped, broker-authorized, or guaranteed not to interfere. The backend restores KWin desktop and focus itself; pointer restoration needs the global-input companion and is reported explicitly.
