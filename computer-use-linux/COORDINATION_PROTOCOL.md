# Desktop Coordination Protocol

Version 2 defines one cross-process contract for window ownership and physical
seat arbitration across Linux desktop backends. It does not standardize
compositor-specific capture, focus, workspace, or crash-recovery transactions.

## State and locks

Implementations use a private `0700` coordination directory and create regular,
non-symlink lock and state files with mode `0600`. The shared state file is
`window-claims.json`; `window-claims.lock` serializes state changes;
`window-locks/<window-key>.lock` serializes one window; and
`pointer-transaction.lock` serializes operations that borrow the real keyboard
or pointer. The legacy filename is retained so rolling upgrades share the same
physical-seat lane.

Acquire the physical-seat lock before the window lock. A window-only operation
acquires only its window lock. State-file access occurs while those operation
locks are held and is additionally serialized by `window-claims.lock`.

Writers use an atomic same-directory replace followed by a directory `fsync`.
Readers reject malformed, oversized, unsupported-version, or identity-mismatched
state. They do not silently discard it.

## Identities

A session identity contains:

- one backend from `cosmic`, `gnome`, `hyprland`, `i3`, `niri`, `plasma`, or
  `x11`;
- the Unix user ID;
- one or more bounded backend-specific endpoint attributes.

Endpoint attributes must identify the live compositor connection, rather than
only its display name. Examples include a socket device/inode, compositor
instance signature, D-Bus owner, or Shell instance identifier.

A window identity contains the same backend, a stable backend-owned identifier,
and an optional process ID/start-time pair. Backends must fail closed when they
cannot distinguish a stale/reused identifier from the claimed window.

Session and window map keys are lowercase SHA-256 digests of compact,
UTF-8 JSON. Object keys are sorted lexicographically and no insignificant
whitespace is included. A window key hashes:

```json
{"session":<session-identity>,"window":<window-identity>}
```

The checked-in fixture is the canonical cross-language test vector.

## Claims

Claims contain opaque owner and token values, a nonzero monotonically increasing
fencing token, bounded Unix-millisecond claim/renewal/expiry deadlines, the
granted lease duration, an optional in-flight deadline, an optional owner
process identity, and a bounded non-authoritative window summary. Expiry must
equal `renewed_at_ms + lease_seconds * 1000`; an in-flight deadline may extend
at most 300 seconds past expiry. Each session records the next fencing token,
which must be greater than every token it has issued.

The owner identity comes from host-provided MCP metadata, never a model argument.
Only the exact token owner may renew, release, or use a live claim, and renewal
must present the existing token. Tokens are returned only when a claim is
created or renewed; listing, diagnostics, errors, and logs must never expose
them. An operation may extend `inflight_until_ms` before dispatch so lease
expiry cannot transfer ownership while delivery is uncertain.

Expired claims are pruned under the state lock. Implementations may additionally
prune a claim whose recorded owner process is provably dead, but failure to
inspect a process is not proof of death.

Window summaries are for diagnostics only. Authorization and lock selection
must use the canonical identities.

## Compatibility

Hyprland companions and the generic backend write version 2. During a rolling upgrade they acquire the legacy address lock alongside the canonical window lock, in lexicographic path order.
A version-1 journal is replaced only after every claim and in-flight deadline is inactive; otherwise lifecycle calls fail closed. No claim is copied between formats. Once version 2 exists it is authoritative, and older writers reject it instead of creating split ownership.
