# Linux Computer Use

This fork owns the complete Codex-on-Linux computer-use integration while
keeping the generic backend independently maintainable.

## Architecture

1. `computer-use-linux/upstream/` contains the provenance-pinned
   `agent-sh/computer-use-linux` engine plus explicitly recorded generic Linux
   patches. It owns screenshots, AT-SPI, input, diagnostics, and window backends.
2. `computer-use-linux/` owns the Codex plugin launcher, build-time GNOME/DBus
   identity, environment compatibility, update policy, and packaging.
3. `computer-use-linux/codex-integration/` owns product-specific Linux code that
   should not enter the generic engine, currently the Chrome native host/runtime.
4. `contrib/*computer-use/` owns compositor-specific same-session background
   capture, targeted input, and recoverable focus leases.
5. `codex-rs/` owns the agent runtime. Future native work belongs there when it
   concerns approvals, scheduling, leases, recovery, or tool orchestration.

## Source policy

`agent-sh/computer-use-linux` is the primary generic upstream.
`ilysenko/codex-desktop-linux/computer-use-linux` is a secondary patch feed for
Linux and Codex-specific work. Exact revisions and locally retained patches are
recorded in `computer-use-linux/UPSTREAM.toml`.

Updates are three-way merged from the pinned primary revision. Local preparation
does not commit, and scheduled automation may only open a verified draft PR.
There is no automatic merge path.

## Deeper Codex integration

Planned runtime work:

1. Define native Linux Computer Use tools behind an OS-specific interface.
2. Reuse the backend without adding Linux dependencies to cross-platform builds.
3. Add a Linux-aware observation/action controller.
4. Add approvals, desktop leases, interruption, recovery, and parallel-worker
   arbitration to the Codex turn loop.

The Hyprland background-control project remains the reference implementation
for focus-preserving Wayland operation. Keep `codex-upstream` as the OpenAI
Codex remote and `hyprland-upstream` as that contributed integration's source.
