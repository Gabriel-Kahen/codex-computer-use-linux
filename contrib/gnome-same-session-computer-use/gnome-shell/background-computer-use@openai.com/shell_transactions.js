import {validateKeyRequest, validatePointerRequest} from './lease_protocol.js';

export function assertInputSafe(status) {
    if (status.locked)
        throw new Error('GNOME session is locked');
    if (status.overviewVisible)
        throw new Error('GNOME overview is open');
    if (status.modalCount > 0)
        throw new Error('a GNOME Shell modal dialog is open');
    if (status.grabbed)
        throw new Error('a compositor move, resize, or input grab is active');
}

export function activateLeaseTransaction(lease, adapter) {
    const target = adapter.findWindow(lease.target);
    if (!target)
        throw new Error(`window ${lease.target} no longer exists`);
    const workspace = adapter.workspaceForWindow(target);
    // Recovery must become authoritative before the first desktop mutation.
    adapter.markLeaseActive();
    if (workspace)
        adapter.activateWorkspace(workspace);
    adapter.unminimizeWindow(target);
    adapter.focusWindow(target);
    const state = adapter.state();
    if (state.focused_window !== lease.target)
        throw new Error('GNOME did not focus the lease target');
    return state;
}

export function restoreLeaseTransaction(lease, adapter) {
    const original = lease.original;
    const leaseTarget = adapter.findWindow(lease.target);
    const missingWindows = [];
    if (leaseTarget && lease.targetMinimized)
        adapter.minimizeWindow(leaseTarget);
    else if (!leaseTarget)
        missingWindows.push(`target:${lease.target}`);

    const focused = original.focused_window
        ? adapter.findWindow(original.focused_window)
        : null;
    if (focused) {
        const workspace = adapter.workspaceForWindow(focused);
        if (workspace)
            adapter.activateWorkspace(workspace);
        adapter.focusWindow(focused);
    } else {
        const workspace = adapter.workspaceByIndex(original.workspace);
        if (workspace)
            adapter.activateWorkspace(workspace);
    }
    if (original.pointer)
        adapter.movePointer(original.pointer);

    const state = adapter.state();
    const errors = [];
    if (original.focused_window && !focused)
        missingWindows.push(`original-focused:${original.focused_window}`);
    else if (state.focused_window !== original.focused_window)
        errors.push(`focused window mismatch: expected ${original.focused_window}, got ${state.focused_window}`);
    if (state.workspace !== original.workspace)
        errors.push(`workspace mismatch: expected ${original.workspace}, got ${state.workspace}`);
    if (!original.pointer || Math.abs(state.pointer.x - original.pointer.x) >= 1 || Math.abs(state.pointer.y - original.pointer.y) >= 1)
        errors.push('pointer restoration mismatch');
    if (leaseTarget && Boolean(adapter.isMinimized(leaseTarget)) !== lease.targetMinimized)
        errors.push('lease target minimized-state mismatch');
    const recoveryComplete = errors.length === 0;
    return {
        // Older brokers only understand `restored`; keep it as the terminal
        // cleanup signal while newer brokers use `missing_windows` for fidelity.
        restored: recoveryComplete,
        recovery_complete: recoveryComplete,
        errors,
        missing_windows: missingWindows,
        state,
    };
}

export function assertFreshWindowFrame(settle, targetId) {
    if (settle?.reason !== 'damaged-and-painted')
        throw new Error(`window ${targetId} did not submit and paint a fresh buffer after unminimize`);
}

export function injectPointerTransaction(request, frame, adapter) {
    validatePointerRequest(request, frame);
    const local = request.action === 'drag' ? request.start : request.point;
    const original = adapter.pointer();
    const move = point => adapter.movePointer({x: frame.x + point.x, y: frame.y + point.y});
    try {
        move(local);
        if (request.action === 'click') {
            for (let i = 0; i < request.count; i++) {
                let pressed = false;
                try {
                    pressed = true;
                    adapter.pressButton(request.button);
                    adapter.releaseButton(request.button);
                    pressed = false;
                } finally {
                    if (pressed)
                        adapter.releaseButton(request.button);
                }
            }
        } else if (request.action === 'scroll') {
            const direction = request.steps > 0 ? 'down' : 'up';
            for (let i = 0; i < Math.abs(request.steps); i++)
                adapter.scroll(direction);
        } else if (request.action === 'drag') {
            let pressed = false;
            try {
                pressed = true;
                adapter.pressButton(request.button);
                for (let i = 1; i <= request.motion_steps; i++) {
                    move({
                        x: local.x + (request.end.x - local.x) * i / request.motion_steps,
                        y: local.y + (request.end.y - local.y) * i / request.motion_steps,
                    });
                }
                adapter.releaseButton(request.button);
                pressed = false;
            } finally {
                if (pressed)
                    adapter.releaseButton(request.button);
            }
        }
    } finally {
        adapter.movePointer(original);
    }
}

export function injectKeyTransaction(request, adapter) {
    validateKeyRequest(request);
    const modifiers = request.modifiers.map(adapter.resolveModifier);
    const key = adapter.resolveKey(request.key);
    const pressedModifiers = [];
    try {
        for (const modifier of modifiers) {
            pressedModifiers.push(modifier);
            adapter.pressKey(modifier);
        }
        let keyPressed = false;
        try {
            keyPressed = true;
            adapter.pressKey(key);
            adapter.releaseKey(key);
            keyPressed = false;
        } finally {
            if (keyPressed)
                adapter.releaseKey(key);
        }
    } finally {
        let releaseError = null;
        for (const modifier of pressedModifiers.reverse()) {
            try {
                adapter.releaseKey(modifier);
            } catch (error) {
                releaseError ??= error;
            }
        }
        if (releaseError)
            throw releaseError;
    }
}
