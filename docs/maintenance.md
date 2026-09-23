# Maintenance and releases

## Boundaries

Maintain the modified Linux engine and desktop plugins. Use public stock Codex
interfaces; do not reintroduce a Codex runtime fork to fix tool output. The
optional Chrome native host is separately built and is not a prerequisite for
desktop control. Upstream attribution and local patch lineage are explicit.

## Weekly updates

`computer-use-upstream-sync` checks the pinned engine against upstream, attempts
a three-way merge, and validates the candidate before opening/updating a draft
PR. It never automatically merges that PR. A conflict, build failure, or native
test failure stays red and opens/updates one maintenance issue. Logs are kept
as workflow artifacts. Fix the issue or review the candidate; do not make the
workflow green by ignoring a repeated failure.

Use `just computer-use-upstream-status` locally. Prepare updates in an isolated
checkout with `just computer-use-upstream-prepare`, preserve local behavior,
review the diff against the recorded upstream revision, and run the full gates.
Secondary desktop-integration changes are selectively ported, never blindly
copied over the primary engine.

Dependabot proposes dependency and action updates separately. Do not conflate
a dependency lockfile update with having incorporated all upstream engine code.
Stock Codex checks should exercise both the pinned tested client and the latest
stable client so a new client regression becomes visible before an upgrade.

## Release procedure

1. Finish and review engine, packaging, and desktop changes. All required CI
   must pass on the release commit.
2. Set `computer-use-linux/PREBUILT_VERSION` and the plugin manifests to the
   new package release version. The engine crate and native protocol versions
   have separate meanings and need not match the package version.
3. Update the changelog, tested compatibility records, and README installation
   version. Document any compositor or client support changes explicitly.
4. Merge to `main`, then tag that exact commit `computer-use-v<VERSION>`.
5. The release workflow reruns its gates, builds x86_64/aarch64 engine bundles,
   verifies archive checksums, attests provenance, and publishes only after all
   required checks pass. Do not publish a manually built replacement asset.
6. Download the published bundle using `scripts/install_release.py`, verify it,
   and check initialization in a fresh Codex task. Preserve a previous bundle
   for rollback. Published release assets are immutable; fix errors in a new version.

Source installations build with `Cargo.lock`; binary installations verify both
embedded executables at startup. Neither follows moving upstream code at runtime.

## Hyprland compatibility

The declared native integration target is Hyprland 0.55.4. Builds are cached by
source and running compositor identity, and loaded extensions must match the
running ABI. Upgrading Hyprland does not grant support automatically: add and run
the isolated compositor test before changing the support claim. Shared engine
capabilities can remain available while native background input is unavailable.

Do not automatically unload a live extension during another task's operation.
Finish tasks, unload the old extension or restart the compositor, then verify
the new companion. See `MIGRATION.md` for rollback.

## Repository settings

Enable Issues, Actions, and workflow-created pull requests. Protect `main` with
the aggregate CI check and native Hyprland check; prefer pull requests to direct
pushes. Release tags must point at reviewed `main` commits. The legacy Codex
fork is retained for history and should not run competing sync automation.
