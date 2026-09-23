# Contributing

This repository maintains Linux desktop plugins and a modified engine for stock
Codex. Codex runtime and desktop-frontend development belongs upstream or in a
separate project. Discuss significant changes in an issue or draft PR first.

## Checks

Run `just check`. Changes to desktop input, capture, or coordination must include
behavioral tests. Native Hyprland tests run in an isolated VM; do not use the
maintainer's active desktop as a test fixture. CI also checks X11/Plasma native
helpers and official Codex integration. A mocked test is not evidence of live
application compatibility.

## Engine changes

Read `computer-use-linux/UPSTREAM.md` before modifying the imported engine.
Keep useful local behavior during three-way merges, update the exact upstream
revision only when integrated, and contribute generic improvements upstream
when possible. Commit Cargo lockfile changes deliberately.

## Releases

Follow `docs/maintenance.md`. Releases are immutable, checksummed, and attested;
all required gates must pass before publication. Keep experimental compositor
support clearly distinguished from the tested support matrix. Never suppress
an unresolved upstream conflict to make automation green.

## Security and licensing

Use `SECURITY.md` for private vulnerability reports. Preserve copyright,
license, and upstream provenance when moving or importing files.
