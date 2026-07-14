import assert from 'node:assert/strict';
import test from 'node:test';

import {LeaseProtocol} from '../gnome-shell/background-computer-use@openai.com/lease_protocol.js';
import {
    activateLeaseTransaction,
    assertInputSafe,
    injectKeyTransaction,
    injectPointerTransaction,
    restoreLeaseTransaction,
} from '../gnome-shell/background-computer-use@openai.com/shell_transactions.js';

const CAPABILITY = 'c'.repeat(64);
const OWNER = ':1.10';
const FRAME = {x: 50, y: 60, width: 800, height: 600};

test('input safety rejects lock, overview, modal, and grab states', () => {
    const safe = {locked: false, overviewVisible: false, modalCount: 0, grabbed: false};
    assert.doesNotThrow(() => assertInputSafe(safe));
    for (const [field, value] of [
        ['locked', true],
        ['overviewVisible', true],
        ['modalCount', 1],
        ['grabbed', true],
    ]) {
        assert.throws(() => assertInputSafe({...safe, [field]: value}));
    }
});

test('activation performs the focus transaction and verifies its result', () => {
    const calls = [];
    const target = {id: '11'};
    const workspace = {index: 2};
    const protocol = new LeaseProtocol();
    const lease = protocol.begin({
        capability: CAPABILITY,
        owner: OWNER,
        target: '11',
        targetMinimized: false,
        original: {workspace: 1},
    });
    const adapter = {
        findWindow: id => (calls.push(`find:${id}`), target),
        workspaceForWindow: window => (calls.push(`workspace:${window.id}`), workspace),
        markLeaseActive: () => {
            calls.push('mark-active');
            protocol.activate(CAPABILITY, OWNER);
        },
        activateWorkspace: value => calls.push(`activate-workspace:${value.index}`),
        unminimizeWindow: window => calls.push(`unminimize:${window.id}`),
        focusWindow: window => calls.push(`focus:${window.id}`),
        state: () => ({focused_window: 'wrong'}),
    };

    assert.throws(() => activateLeaseTransaction(lease, adapter), /did not focus/);
    assert.equal(protocol.lease.phase, 'active');
    assert.deepEqual(calls, [
        'find:11',
        'workspace:11',
        'mark-active',
        'activate-workspace:2',
        'unminimize:11',
        'focus:11',
    ]);
});

test('restore runs every step and a postcondition mismatch retains the lease', () => {
    const calls = [];
    const target = {id: '11', minimized: false};
    const focused = {id: '22'};
    const workspace = {index: 3};
    const original = {
        focused_window: '22',
        workspace: 3,
        pointer: {x: 10, y: 20},
    };
    const protocol = new LeaseProtocol();
    const lease = protocol.begin({
        capability: CAPABILITY,
        owner: OWNER,
        target: '11',
        targetMinimized: true,
        original,
    });
    protocol.activate(CAPABILITY, OWNER);
    const result = restoreLeaseTransaction(lease, {
        findWindow: id => (calls.push(`find:${id}`), id === '11' ? target : focused),
        workspaceForWindow: window => (calls.push(`workspace:${window.id}`), workspace),
        workspaceByIndex: index => (calls.push(`workspace-index:${index}`), workspace),
        activateWorkspace: value => calls.push(`activate-workspace:${value.index}`),
        focusWindow: window => calls.push(`focus:${window.id}`),
        minimizeWindow: window => {
            calls.push(`minimize:${window.id}`);
            window.minimized = true;
        },
        isMinimized: window => (calls.push(`is-minimized:${window.id}`), window.minimized),
        movePointer: point => calls.push(`pointer:${point.x},${point.y}`),
        state: () => ({focused_window: 'wrong', workspace: 3, pointer: {x: 10, y: 20}}),
    });
    protocol.finishRestore(result.restored);

    assert.equal(result.restored, false);
    assert.match(result.errors.join('\n'), /focused window mismatch/);
    assert.equal(protocol.lease.phase, 'active');
    assert.deepEqual(calls, [
        'find:11',
        'minimize:11',
        'find:22',
        'workspace:22',
        'activate-workspace:3',
        'focus:22',
        'pointer:10,20',
        'is-minimized:11',
    ]);
});

for (const action of ['click', 'drag']) {
    test(`${action} releases a pressed button when the virtual device throws`, () => {
        const calls = [];
        const request = action === 'click'
            ? {action, button: 'left', count: 1, point: {x: 1, y: 2}}
            : {action, button: 'left', motion_steps: 2, start: {x: 1, y: 2}, end: {x: 3, y: 4}};
        const adapter = {
            pointer: () => ({x: 100, y: 200}),
            movePointer: point => calls.push(`move:${point.x},${point.y}`),
            pressButton: button => {
                calls.push(`press:${button}`);
                throw new Error('virtual device failed after press');
            },
            releaseButton: button => calls.push(`release:${button}`),
            scroll: () => assert.fail('unexpected scroll'),
        };

        assert.throws(() => injectPointerTransaction(request, FRAME, adapter), /failed after press/);
        assert.deepEqual(calls, [
            'move:51,62',
            'press:left',
            'release:left',
            'move:100,200',
        ]);
    });
}

test('modifier press failure releases every modifier that may be down', () => {
    const calls = [];
    const adapter = {
        resolveModifier: name => name,
        resolveKey: name => name,
        pressKey: key => {
            calls.push(`press:${key}`);
            if (key === 'SHIFT')
                throw new Error('modifier press failed');
        },
        releaseKey: key => calls.push(`release:${key}`),
    };

    assert.throws(
        () => injectKeyTransaction({key: 'X', modifiers: ['CTRL', 'SHIFT']}, adapter),
        /modifier press failed/,
    );
    assert.deepEqual(calls, ['press:CTRL', 'press:SHIFT', 'release:SHIFT', 'release:CTRL']);
});

for (const failure of ['press', 'release']) {
    test(`key ${failure} failure releases the key and already-pressed modifiers`, () => {
        const calls = [];
        let failed = false;
        const adapter = {
            resolveModifier: name => name,
            resolveKey: name => name,
            pressKey: key => {
                calls.push(`press:${key}`);
                if (failure === 'press' && key === 'X')
                    throw new Error('key press failed');
            },
            releaseKey: key => {
                calls.push(`release:${key}`);
                if (failure === 'release' && key === 'X' && !failed) {
                    failed = true;
                    throw new Error('key release failed');
                }
            },
        };

        assert.throws(
            () => injectKeyTransaction({key: 'X', modifiers: ['CTRL']}, adapter),
            new RegExp(`key ${failure} failed`),
        );
        assert.deepEqual(calls, failure === 'press'
            ? ['press:CTRL', 'press:X', 'release:X', 'release:CTRL']
            : ['press:CTRL', 'press:X', 'release:X', 'release:X', 'release:CTRL']);
    });
}
