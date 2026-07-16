import assert from 'node:assert/strict';
import test from 'node:test';

import {
    LeaseProtocol,
    validateKeyRequest,
    validatePointerRequest,
} from '../gnome-shell/background-computer-use@openai.com/lease_protocol.js';
import {
    beginClaimedLeaseAsync,
    renewLeaseAsync,
} from '../gnome-shell/background-computer-use@openai.com/lease_dbus.js';

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

function dbusInvocation(sender = OWNER) {
    return {
        error: null,
        result: null,
        get_sender: () => sender,
        return_dbus_error(name, message) {
            this.error = {name, message};
        },
        return_value(value) {
            this.result = value;
        },
    };
}

const REPLY_ADAPTERS = {
    encodeReply: value => ({encoded: value}),
    errorName: 'org.example.Error',
    monotonicTimeUsec: () => 2_000_000,
};

test('BeginClaimedLeaseAsync parses recovery bounds and propagates the D-Bus sender', () => {
    const invocation = dbusInvocation();
    const calls = [];
    beginClaimedLeaseAsync(['11', '1.25'], invocation, {
        ...REPLY_ADAPTERS,
        beginLease(...args) {
            calls.push(args);
            return {capability: CAPABILITY};
        },
    });

    assert.deepEqual(calls, [['11', OWNER, 3_250_000]]);
    assert.deepEqual(invocation.result, {encoded: {capability: CAPABILITY}});
    assert.equal(invocation.error, null);
});

test('BeginClaimedLeaseAsync rejects invalid and overflowing recovery deadlines by D-Bus reply', () => {
    const invalid = ['', '0', '-1', '300.000001', 'Infinity', 'not-a-number'];
    for (const recoverySeconds of invalid) {
        const invocation = dbusInvocation();
        let called = false;
        beginClaimedLeaseAsync(['11', recoverySeconds], invocation, {
            ...REPLY_ADAPTERS,
            beginLease() {
                called = true;
            },
        });
        assert.equal(called, false);
        assert.equal(invocation.result, null);
        assert.match(invocation.error.message, /greater than 0 and at most 300/);
    }

    const invocation = dbusInvocation();
    beginClaimedLeaseAsync(['11', '1'], invocation, {
        ...REPLY_ADAPTERS,
        beginLease() {},
        monotonicTimeUsec: () => Number.MAX_SAFE_INTEGER,
    });
    assert.match(invocation.error.message, /outside the supported range/);
});

test('BeginClaimedLeaseAsync converts callback failures to D-Bus error replies', () => {
    const invocation = dbusInvocation();
    beginClaimedLeaseAsync(['11', '5'], invocation, {
        ...REPLY_ADAPTERS,
        beginLease() {
            throw new Error('lease preparation failed');
        },
    });

    assert.deepEqual(invocation.error, {
        name: REPLY_ADAPTERS.errorName,
        message: 'lease preparation failed',
    });
    assert.equal(invocation.result, null);
});

test('RenewLeaseAsync propagates capability, sender, and the monotonic deadline', () => {
    const invocation = dbusInvocation(OTHER_OWNER);
    const calls = [];
    renewLeaseAsync([CAPABILITY, '0.000001'], invocation, {
        ...REPLY_ADAPTERS,
        renewLease(...args) {
            calls.push(args);
            return {recoveryDeadlineUsec: args[2]};
        },
    });

    assert.deepEqual(calls, [[CAPABILITY, OTHER_OWNER, 2_000_001]]);
    assert.deepEqual(invocation.result, {
        encoded: {renewed: true, recovery_deadline_usec: 2_000_001},
    });
    assert.equal(invocation.error, null);
});

test('RenewLeaseAsync reports parse and protocol authorization failures', () => {
    const invalidInvocation = dbusInvocation();
    renewLeaseAsync([CAPABILITY, '301'], invalidInvocation, {
        ...REPLY_ADAPTERS,
        renewLease() {
            assert.fail('renew must not run for invalid recovery bounds');
        },
    });
    assert.match(invalidInvocation.error.message, /greater than 0 and at most 300/);

    const deniedInvocation = dbusInvocation(OTHER_OWNER);
    renewLeaseAsync([CAPABILITY, '5'], deniedInvocation, {
        ...REPLY_ADAPTERS,
        renewLease() {
            throw new Error('another D-Bus caller owns the lease');
        },
    });
    assert.deepEqual(deniedInvocation.error, {
        name: REPLY_ADAPTERS.errorName,
        message: 'another D-Bus caller owns the lease',
    });
});

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
