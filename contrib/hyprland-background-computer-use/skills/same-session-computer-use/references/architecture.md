# Linux same-session architecture

## Invariants

- Attach to the current user's real Wayland, Hyprland, D-Bus, and AT-SPI sessions.
- Reuse existing application processes and profiles.
- Keep the physical workspace, focused window, and pointer unchanged for normal operations.
- Treat a before/after physical-state mismatch as a failed targeted operation.
  The broker reports which fields changed but does not restore a stale snapshot
  that may reflect concurrent user input.
- Prefer exact window capture and semantic accessibility actions.
- Restore all compositor state after any fallback transaction.
- Fence every claimed window by the host-provided Codex task identity and an expiring token.
- Allow unrelated window-local work to proceed without a process-global broker lock.

## Capability map

| Need | Primary route | Interference |
|---|---|---|
| List real windows | `hyprctl clients -j` | None |
| Claim a window | Atomic private claim store | None |
| Capture one window | `grim -T <stableId>` | None |
| Click an accessible control | AT-SPI `DoAction` | None |
| Edit accessible text/value | AT-SPI EditableText/Value | None |
| Send a discrete key | `hl.dsp.send_shortcut` by window address | None |
| Coordinate click/scroll/drag (Wayland) | Hyprland target-surface injector | None observed |
| Coordinate click/scroll/drag (XWayland) | XWayland-internal XTEST pointer | None observed |
| Unsupported client fallback | Snapshot, focus, inject, restore | Possible brief contention |

## Ownership and lock hierarchy

The broker trusts only `tools/call.params._meta.threadId` as the owner identity. It never reads an owner from tool arguments. Claim records are scoped to the current UID, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, and `HYPRLAND_INSTANCE_SIGNATURE`; written with mode `0600`; and replaced atomically under a cross-process file lock.

Acquire coordination resources in this order:

1. The global input lane, only for XWayland, physical-seat, or fallback work.
2. The target window lane.
3. The short-lived coordinate-lease and claim-state locks.

Native Wayland windows have independent broker lanes, so different agents can issue captures and targeted actions concurrently. The same window is exclusive across broker processes, and the generic Computer Use server shares the lane for capture and mutation. Its focus changes, physical-seat mutations, and screenshots that raise their target take the global input lane before the window lane, while `raise_window=false` exact capture remains window-local. Claim, renewal, release, expiry takeover, capture, focus, semantic actions, input, and combined action-and-observe calls use those locks, preventing claim-expiry and focus/input time-of-check/time-of-use races. Generic calls present the broker-issued owner and fencing token; untargeted desktop capture and global input fail closed while any claim is live.

Claims last 60 seconds by default and accept 5–300 seconds. Successful claimed broker operations renew ownership, and explicit same-owner renewal preserves the fencing token. Foreign captures and mutations are rejected while a claim is live even when they omit a token; an explicitly supplied token must also match the window and owner. The token-only release operation is idempotent after release or expiry and cannot release another task's live claim.

## Headless transaction

Use only for a client that rejects targeted pointer injection or for an explicitly requested dedicated view:

1. Verify the target's claim and reserve the global input lane for the owning task.
2. Create a named Hyprland headless output.
3. Record the owner, claim, display/Hyprland binding, target window's address, workspace, monitor, fullscreen state, the current active window/workspace, and pointer coordinates.
4. Move the existing window with `follow = false`.
5. If it does not already cover the fallback screen, set compositor fullscreen mode while keeping its client fullscreen request unchanged.
6. Perform the shortest possible operation batch.
7. Restore the original fullscreen modes before returning the window to its workspace.
8. Remove the headless output.
9. Restore pointer and focus if either changed.

Run cleanup even after timeouts or errors. Only the owning task may capture, end, or recover a live lease; after the owner deadline and associated claim expire, another task may recover the orphan. End the fallback before releasing its window claim. Never close or relaunch the leased application as cleanup.

## Honest boundary

Stock Hyprland still exposes one physical compositor seat. The targeted-pointer extension avoids that limitation by delivering an atomic event sequence directly to a selected client surface and restoring pointer focus before the next physical event is processed. Hyprland itself handles these requests on its compositor thread, but the broker no longer serializes unrelated native windows, so workers can make independent progress around capture, inspection, and tool I/O. XWayland uses its own internal XTEST pointer and restores it after each action, so this broker globally serializes its XWayland transactions. This is not a set of independent hardware seats, and arbitrary same-user processes are outside the lock; it is broker-enforced window concurrency plus a broker-local global-input boundary.

The broker treats the native extension identity as part of that safety boundary. The extension reports its embedded version, source digest, build-time Hyprland version digest, and build/runtime Hyprland ABI hashes. The broker caches only the immutable expected identity under the active Hyprland-instance/Wayland-socket key and sends it with every native action. The compositor-thread transaction rejects an identity or runtime-ABI mismatch before input, then performs live safety checks, the complete action, pointer-focus restoration, and before/after physical-state comparison without another compositor event interleaving those steps.
