---
name: plasma-same-session-computer-use
description: Operate one or more applications in the user's existing KDE Plasma Wayland session with parallel exact capture, per-window agent claims, and a serialized global-seat fallback.
---

# Plasma same-session computer use

Use this companion with `computer-use-linux@codex-computer-use-linux`. It adds KWin window identity, cross-process ownership, parallel exact compositor capture, and state recovery; the repository-owned plugin owns AT-SPI and global input.

1. Call `plasma_session_status`. Do not claim a capability whose boolean is false.
2. Call `list_plasma_windows` and keep each stable KWin UUID. Re-resolve after a window closes or restarts. For parallel work, assign at most one worker to each window.
3. Each worker calls `claim_session_window` for its own UUID. Keep its `claim_token` private to that worker, pass it to every broker action on the claimed window, renew before `expires_at` by claiming the same window with that token, and call `release_session_window` when finished. Use the paginated `list_window_claims` records to coordinate; they expose owner task IDs but never tokens.
4. Prefer AT-SPI semantic inspection and actions from Computer Use. They usually avoid focus changes and may run concurrently on different claimed windows. Claims are enforced only by this Plasma broker's tools; the separate plugin cannot inspect or enforce them, so never use external AT-SPI or input on a window owned by another task.
5. Use the read-only `get_plasma_window_capture` with `claim_token` for inline exact visual verification. Use `save_plasma_window_capture` only when the user needs a PNG written to an absolute path, and do not use the deprecated `capture_plasma_window` compatibility tool in new workflows. Different windows capture concurrently. If only `exact_capture_transport_available` is true, one first capture attempt is allowed to test KWin authorization; report rejection and do not substitute a desktop screenshot.
6. Only when AT-SPI cannot perform an action, call `begin_plasma_focus_lease` with the claim token and `acknowledge_interference: true`. Plasma has one physical input seat, so focus/global-input fallbacks serialize even while other workers continue capture or non-seat work. Tell the user that focus, desktop, keyboard, and pointer may visibly change.
7. Immediately before every separate Computer Use global-input action, call `validate_plasma_focus_lease`. Begin only when `advisory_ready` is true. The broker reserves the target claim until restoration finishes and enforces one owner-bound focus journal for its own calls, but the separate input tool cannot consume its token; this final boundary is caller policy.
8. Keep each global-seat action short. Never interact with lock screens, password prompts, authentication agents, anti-cheat software, or security boundaries.
9. If pointer input was used, read `pointer_before` and move the pointer back with Computer Use before ending the lease when possible. The owning task must call `end_plasma_focus_lease`, including after a failed action.
10. Call `recover_plasma_focus_lease` as the owner after interruption. A different task must not recover live work; it may recover only after the lease expires, the owning MCP process exits, or the recorded KWin session changes.

KWin does not expose a stable public interface for arbitrary input directly to an inactive surface. Never describe companion global input as parallel, background, invisible, isolated, lease-scoped, broker-authorized, or guaranteed not to interfere. Parallelism applies to different-window claims, exact capture, and non-seat work. The backend restores KWin desktop and focus itself; pointer restoration needs the global-input companion and is reported explicitly.
