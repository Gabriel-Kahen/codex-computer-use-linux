# Moving from the Codex fork

The standalone project retains the name `Gabriel-Kahen/codex-computer-use-linux`.
The original Codex fork and its history are retained at
[`codex-computer-use-linux-legacy`](https://github.com/Gabriel-Kahen/codex-computer-use-linux-legacy).
The extraction starts from `e8d4ff733c6caf04f16c47260c708000270050d7`.

## What changes

- Install a normal, supported Codex client. This project no longer builds or
  updates the Codex CLI, app server, or graphical application.
- The modified Linux engine, four desktop companions, operating skills, and
  optional Chrome host remain here. The engine is not replaced with unmodified upstream.
- Marketplace and plugin identifiers remain unchanged. Existing snapshots
  keep working until deliberately replaced; changing a GitHub repository does
  not update an already installed plugin.
- The legacy fork's runtime patches are historical reference, not patches to
  apply blindly to a current Codex release. See [stock Codex compatibility](docs/stock-codex.md).

## Existing Git checkouts

An old clone contains the full Codex history. Do not pull the standalone
repository into it or merge the unrelated histories. Point that old clone at
the legacy URL and clone the standalone project separately:

```sh
git remote set-url origin https://github.com/Gabriel-Kahen/codex-computer-use-linux-legacy.git
git clone https://github.com/Gabriel-Kahen/codex-computer-use-linux.git ../codex-computer-use-standalone
```

## Existing Codex installations

Finish active computer-use tasks before switching versions. A loaded Hyprland
native extension belongs to its build and compositor ABI: unload it using the
old companion's documented procedure, or restart the Hyprland session before
using the new companion. Do not forcibly recover another task's active lease.

1. Download and verify the chosen release using the README instructions.
2. Record `codex plugin list` and `codex plugin marketplace list --json` so the
   previous versions and local bundle path are available for rollback.
3. Remove only the installed plugins from this marketplace, then remove the
   `codex-computer-use-linux` marketplace registration. Keep the old bundle on disk.
4. Add the verified new bundle as the marketplace and reinstall the shared
   plugin plus your desktop companion using the same plugin identifiers.
5. Start a new Codex task and run `doctor`/the companion's status tool.

For a typical Hyprland installation, step 3 is:

```sh
codex plugin remove same-session-computer-use@codex-computer-use-linux
codex plugin remove computer-use-linux@codex-computer-use-linux
codex plugin marketplace remove codex-computer-use-linux
```

If other companions are installed from this marketplace, remove and reinstall
those as well. Do not remove unrelated plugins. The older bundled
`computer-use@openai-bundled` and this project's shared plugin expose the same
MCP server name and must not both be enabled.

## Rollback

Use the same procedure to re-register the retained previous release directory.
No engine data migration is performed by installing a bundle. Live claims and
leases are runtime state, so finish tasks and handle loaded compositor
extensions before either an upgrade or rollback.

## Provenance

All extracted source retains its original copyright and license. The initial
extraction is a new Git root to avoid carrying the full Codex repository; the
legacy commit above is the source of truth for earlier history. Upstream engine
history and the retained patch lineage are recorded in
[`computer-use-linux/UPSTREAM.toml`](computer-use-linux/UPSTREAM.toml).
