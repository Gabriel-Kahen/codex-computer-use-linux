import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import {
    bridgeInfo,
    operationIdentity,
    parseBridgeContract,
    stableWindowId,
} from '../gnome_shell_bridge_contract.js';

const serialized = fs.readFileSync(
    new URL('../gnome_shell_bridge_contract.json', import.meta.url), 'utf8');
const contract = parseBridgeContract(serialized);
const genericContractUrl = new URL(
    '../../../computer-use-linux/upstream/gnome-shell-extension/' +
    'computer-use-linux@avifenesh.dev/gnome_shell_bridge_contract.json',
    import.meta.url,
);
const genericModuleUrl = new URL(
    '../../../computer-use-linux/upstream/gnome-shell-extension/' +
    'computer-use-linux@avifenesh.dev/gnome_shell_bridge_contract.js',
    import.meta.url,
);

test('one contract defines bridge, window, operation, and claim-fence identity', () => {
    assert.deepEqual(bridgeInfo(contract, 'test-role', ['capture']), {
        ...contract,
        role: 'test-role',
        features: ['capture'],
    });
    assert.equal(stableWindowId({get_stable_sequence: () => 17}), '17');
    assert.deepEqual(operationIdentity(
        contract,
        'shell-a',
        {get_stable_sequence: () => 17},
        'act-and-capture',
        'g'.repeat(64),
    ), {
        contract_version: 1,
        shell_instance: 'shell-a',
        window: {scheme: 'meta-stable-sequence-v1', id: '17'},
        kind: 'act-and-capture',
        generation: 'g'.repeat(64),
    });
});

test('invalid identities fail closed', () => {
    assert.throws(() => parseBridgeContract('{}'), /contract is invalid/);
    assert.throws(() => stableWindowId({get_stable_sequence: () => Number.NaN}), /stable sequence/);
    assert.throws(() => operationIdentity(contract, 'shell-a', {
        get_stable_sequence: () => 17,
    }, 'capture', 'short'), /operation identity/);
});

test('the generic extension package carries the same contract assets', {
    skip: !fs.existsSync(genericContractUrl),
}, () => {
    assert.equal(fs.readFileSync(genericContractUrl, 'utf8'), serialized);
    assert.equal(
        fs.readFileSync(genericModuleUrl, 'utf8'),
        fs.readFileSync(new URL('../gnome_shell_bridge_contract.js', import.meta.url), 'utf8'),
    );
});
