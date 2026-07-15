import assert from 'node:assert/strict';
import test from 'node:test';

import {
    LeaseProtocol,
    validateKeyRequest,
    validatePointerRequest,
} from '../gnome-shell/background-computer-use@openai.com/lease_protocol.js';

const CAPABILITY = 'c'.repeat(64);
const OTHER_CAPABILITY = 'x'.repeat(64);
const OWNER = ':1.10';
const OTHER_OWNER = ':1.20';
const FRAME = {width: 800, height: 600};

function pendingProtocol() {
    const protocol = new LeaseProtocol();
    protocol.begin({
        capability: CAPABILITY,
        owner: OWNER,
        target: '11',
        targetMinimized: false,
        original: {workspace: 2},
    });
    return protocol;
}

test('capability and unique caller jointly authorize a lease', () => {
    const protocol = pendingProtocol();
    assert.throws(() => protocol.require(OTHER_CAPABILITY, OWNER), /invalid Shell lease capability/);
    assert.throws(() => protocol.require(CAPABILITY, OTHER_OWNER), /another D-Bus caller/);
    assert.equal(protocol.require(CAPABILITY, OWNER).phase, 'pending');
});

test('phase gates activation and active input authorization', () => {
    const protocol = pendingProtocol();
    assert.throws(() => protocol.require(CAPABILITY, OWNER, 'active'), /must be active/);
    assert.equal(protocol.activate(CAPABILITY, OWNER).phase, 'active');
    assert.equal(protocol.require(CAPABILITY, OWNER, 'active').target, '11');
    assert.throws(() => protocol.activate(CAPABILITY, OWNER), /must be pending/);
});

test('recovery cannot steal a live caller but can bind after it vanishes', () => {
    const protocol = pendingProtocol();
    protocol.activate(CAPABILITY, OWNER);
    assert.throws(() => protocol.recover(CAPABILITY, OTHER_OWNER, true), /still connected/);
    assert.equal(protocol.recover(CAPABILITY, OTHER_OWNER, false).owner, OTHER_OWNER);
    assert.throws(() => protocol.require(CAPABILITY, OWNER), /another D-Bus caller/);
});

test('claimed lease recovery waits for expiry even when capability is known', () => {
    const protocol = new LeaseProtocol();
    protocol.begin({
        capability: CAPABILITY,
        owner: OWNER,
        target: '11',
        targetMinimized: false,
        original: {workspace: 2},
        recoveryDeadlineUsec: 2_000_000,
    });
    protocol.activate(CAPABILITY, OWNER);

    assert.throws(
        () => protocol.recover(CAPABILITY, OTHER_OWNER, true, 1_999_999),
        /still connected/,
    );
    assert.equal(protocol.recover(CAPABILITY, OTHER_OWNER, true, 2_000_000).owner, OTHER_OWNER);
});

test('only the current D-Bus owner can renew claimed recovery fencing', () => {
    const protocol = pendingProtocol();
    assert.throws(
        () => protocol.renew(CAPABILITY, OTHER_OWNER, 3_000_000),
        /another D-Bus caller/,
    );
    assert.equal(protocol.renew(CAPABILITY, OWNER, 3_000_000).recoveryDeadlineUsec, 3_000_000);
});

test('failed restore retains state while successful restore clears it', () => {
    const protocol = pendingProtocol();
    protocol.activate(CAPABILITY, OWNER);
    protocol.finishRestore(false);
    assert.equal(protocol.lease.phase, 'active');
    protocol.finishRestore(true);
    assert.equal(protocol.lease, null);
});

test('only an unactivated pending lease expires', () => {
    const pending = pendingProtocol();
    pending.expirePending();
    assert.equal(pending.lease, null);

    const active = pendingProtocol();
    active.activate(CAPABILITY, OWNER);
    active.expirePending();
    assert.equal(active.lease.phase, 'active');
});

test('pointer validation accepts bounded click, scroll, and drag requests', () => {
    assert.equal(validatePointerRequest({action: 'click', button: 'left', count: 3, point: {x: 0, y: 0}}, FRAME).count, 3);
    assert.equal(validatePointerRequest({action: 'scroll', button: 'left', steps: -20, point: {x: 799, y: 599}}, FRAME).steps, -20);
    assert.equal(validatePointerRequest({action: 'drag', button: 'right', motion_steps: 32, start: {x: 1, y: 2}, end: {x: 3, y: 4}}, FRAME).motion_steps, 32);
});

test('pointer validation rejects non-finite, out-of-frame, and unbounded values', () => {
    const invalid = [
        {action: 'click', button: 'left', count: 1, point: {x: Number.NaN, y: 0}},
        {action: 'click', button: 'left', count: 4, point: {x: 0, y: 0}},
        {action: 'scroll', button: 'left', steps: 0, point: {x: 0, y: 0}},
        {action: 'drag', button: 'left', motion_steps: 1, start: {x: 0, y: 0}, end: {x: 1, y: 1}},
        {action: 'click', button: 'left', count: 1, point: {x: 800, y: 0}},
    ];
    for (const request of invalid)
        assert.throws(() => validatePointerRequest(request, FRAME));
});

test('key validation enforces bounded keys and unique known modifiers', () => {
    assert.equal(validateKeyRequest({key: 'RETURN', modifiers: ['CTRL', 'SHIFT']}).key, 'RETURN');
    assert.throws(() => validateKeyRequest({key: '', modifiers: []}), /key must contain/);
    assert.throws(() => validateKeyRequest({key: 'x'.repeat(65), modifiers: []}), /key must contain/);
    assert.throws(() => validateKeyRequest({key: 'x', modifiers: ['CTRL', 'CTRL']}), /unique subset/);
    assert.throws(() => validateKeyRequest({key: 'x', modifiers: ['HYPER']}), /unique subset/);
});
