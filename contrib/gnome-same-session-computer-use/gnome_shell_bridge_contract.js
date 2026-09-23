export const CONTRACT_FILENAME = 'gnome_shell_bridge_contract.json';

const REQUIRED_STRING_FIELDS = [
    'family',
    'window_identity',
    'operation_identity',
    'claim_fence',
];

export function parseBridgeContract(serialized) {
    const contract = JSON.parse(serialized);
    if (!contract || typeof contract !== 'object' ||
        !Number.isInteger(contract.contract_version) || contract.contract_version < 1 ||
        REQUIRED_STRING_FIELDS.some(field =>
            typeof contract[field] !== 'string' || contract[field].length === 0))
        throw new Error('GNOME Shell bridge contract is invalid');
    return Object.freeze({...contract});
}

export function stableWindowId(window) {
    const sequence = window?.get_stable_sequence?.();
    if (!Number.isSafeInteger(sequence) || sequence < 0)
        throw new Error('window has no valid Meta stable sequence');
    return String(sequence);
}

export function bridgeInfo(contract, role, features) {
    if (typeof role !== 'string' || !role)
        throw new Error('GNOME Shell bridge role is required');
    return {
        ...contract,
        role,
        features: [...features],
    };
}

export function operationIdentity(
    contract,
    shellInstance,
    window,
    kind,
    generation = null,
) {
    if (typeof shellInstance !== 'string' || !shellInstance ||
        typeof kind !== 'string' || !kind ||
        generation !== null && (typeof generation !== 'string' || generation.length < 32))
        throw new Error('GNOME Shell operation identity is invalid');
    return {
        contract_version: contract.contract_version,
        shell_instance: shellInstance,
        window: {
            scheme: contract.window_identity,
            id: stableWindowId(window),
        },
        kind,
        generation,
    };
}
