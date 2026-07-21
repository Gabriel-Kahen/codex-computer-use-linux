const ACTIONS = new Set(['click', 'scroll', 'drag']);
const BUTTONS = new Set(['left', 'right', 'middle']);
const MODIFIERS = new Set(['CTRL', 'SHIFT', 'ALT', 'SUPER']);

function boundedInteger(value, minimum, maximum, excludeZero = false) {
    return Number.isInteger(value) && value >= minimum && value <= maximum && (!excludeZero || value !== 0);
}

function checkPoint(point, frame) {
    if (!point || typeof point !== 'object' || !Number.isFinite(point.x) || !Number.isFinite(point.y) ||
        point.x < 0 || point.y < 0 || point.x >= frame.width || point.y >= frame.height)
        throw new Error(`coordinate (${point?.x},${point?.y}) is outside ${frame.width}x${frame.height}`);
}

export function validatePointerRequest(request, frame) {
    if (!request || typeof request !== 'object' || !ACTIONS.has(request.action))
        throw new Error('pointer action must be click, scroll, or drag');
    if (!BUTTONS.has(request.button))
        throw new Error('button must be left, right, or middle');
    if (!frame || !Number.isFinite(frame.width) || !Number.isFinite(frame.height) || frame.width <= 0 || frame.height <= 0)
        throw new Error('window frame must have finite positive dimensions');
    if (request.action === 'click' && !boundedInteger(request.count, 1, 3))
        throw new Error('count must be an integer between 1 and 3');
    if (request.action === 'scroll' && !boundedInteger(request.steps, -20, 20, true))
        throw new Error('steps must be a non-zero integer between -20 and 20');
    if (request.action === 'drag' && !boundedInteger(request.motion_steps, 2, 32))
        throw new Error('motion_steps must be an integer between 2 and 32');
    checkPoint(request.action === 'drag' ? request.start : request.point, frame);
    if (request.action === 'drag')
        checkPoint(request.end, frame);
    return request;
}

export function validateKeyRequest(request) {
    if (!request || typeof request !== 'object' || typeof request.key !== 'string' ||
        request.key.length === 0 || request.key.length > 64)
        throw new Error('key must contain between 1 and 64 characters');
    if (!Array.isArray(request.modifiers) || request.modifiers.length > 4 ||
        request.modifiers.some(name => typeof name !== 'string' || !MODIFIERS.has(name)) ||
        new Set(request.modifiers).size !== request.modifiers.length)
        throw new Error('modifiers must be a unique subset of CTRL, SHIFT, ALT, and SUPER');
    return request;
}

export class LeaseProtocol {
    constructor() {
        this.lease = null;
    }

    begin({
        capability,
        owner,
        target,
        targetMinimized,
        original,
        generation,
        recoveryDeadlineUsec = null,
    }) {
        if (this.lease)
            throw new Error('a Shell lease is already pending or active');
        if (typeof capability !== 'string' || capability.length < 64)
            throw new Error('invalid Shell lease capability');
        if (typeof owner !== 'string' || !owner)
            throw new Error('invalid D-Bus lease owner');
        if (typeof target !== 'string' || !target)
            throw new Error('invalid lease target');
        if (typeof generation !== 'string' || generation.length < 32)
            throw new Error('invalid lease generation');
        if (recoveryDeadlineUsec !== null &&
            (!Number.isSafeInteger(recoveryDeadlineUsec) || recoveryDeadlineUsec <= 0))
            throw new Error('invalid lease recovery deadline');
        this.lease = {
            capability,
            owner,
            target,
            targetMinimized: Boolean(targetMinimized),
            original,
            generation,
            recoveryDeadlineUsec,
            phase: 'pending',
        };
        return this.lease;
    }

    require(capability, owner, phase = null) {
        if (typeof capability !== 'string' || capability.length < 64 ||
            !this.lease || capability !== this.lease.capability)
            throw new Error('invalid Shell lease capability');
        if (owner !== this.lease.owner)
            throw new Error('Shell lease capability belongs to another D-Bus caller');
        if (phase && this.lease.phase !== phase)
            throw new Error(`Shell lease must be ${phase}, not ${this.lease.phase}`);
        return this.lease;
    }

    activate(capability, owner) {
        const lease = this.require(capability, owner, 'pending');
        lease.phase = 'active';
        return lease;
    }

    renew(capability, owner, recoveryDeadlineUsec) {
        const lease = this.require(capability, owner);
        if (!Number.isSafeInteger(recoveryDeadlineUsec) || recoveryDeadlineUsec <= 0)
            throw new Error('invalid lease recovery deadline');
        lease.recoveryDeadlineUsec = recoveryDeadlineUsec;
        return lease;
    }

    recover(capability, owner, originalOwnerPresent, nowUsec = null) {
        if (typeof capability !== 'string' || capability.length < 64 ||
            !this.lease || capability !== this.lease.capability)
            throw new Error('invalid Shell lease capability');
        if (owner !== this.lease.owner) {
            const claimLive = this.lease.recoveryDeadlineUsec === null ||
                nowUsec === null || nowUsec < this.lease.recoveryDeadlineUsec;
            if (originalOwnerPresent && claimLive)
                throw new Error('the original D-Bus lease owner is still connected');
            this.lease.owner = owner;
        }
        return this.lease;
    }

    finishRestore(recoveryComplete) {
        if (recoveryComplete)
            this.lease = null;
    }

    expirePending() {
        if (this.lease?.phase === 'pending')
            this.lease = null;
    }

    clear() {
        this.lease = null;
    }
}
