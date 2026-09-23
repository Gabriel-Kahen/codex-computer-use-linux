const MAX_RECOVERY_SECONDS = 300;

function recoveryDeadlineUsec(value, nowUsec) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds > MAX_RECOVERY_SECONDS)
        throw new Error('claimed lease recovery seconds must be a number greater than 0 and at most 300');
    if (!Number.isSafeInteger(nowUsec) || nowUsec < 0)
        throw new Error('monotonic time must be a non-negative safe integer');
    const deadline = nowUsec + Math.ceil(seconds * 1_000_000);
    if (!Number.isSafeInteger(deadline))
        throw new Error('claimed lease recovery deadline is outside the supported range');
    return deadline;
}

function reply(invocation, callback, encodeReply, errorName) {
    try {
        invocation.return_value(encodeReply(callback()));
    } catch (error) {
        invocation.return_dbus_error(errorName, String(error.message ?? error));
    }
}

export function beginClaimedLeaseAsync(
    [id, recoverySeconds],
    invocation,
    {beginLease, encodeReply, errorName, monotonicTimeUsec},
) {
    reply(invocation, () => beginLease(
        id,
        invocation.get_sender(),
        recoveryDeadlineUsec(recoverySeconds, monotonicTimeUsec()),
    ), encodeReply, errorName);
}

export function renewLeaseAsync(
    [capability, recoverySeconds],
    invocation,
    {encodeReply, errorName, monotonicTimeUsec, renewLease},
) {
    reply(invocation, () => {
        const deadline = recoveryDeadlineUsec(recoverySeconds, monotonicTimeUsec());
        const lease = renewLease(capability, invocation.get_sender(), deadline);
        return {renewed: true, recovery_deadline_usec: lease.recoveryDeadlineUsec};
    }, encodeReply, errorName);
}
