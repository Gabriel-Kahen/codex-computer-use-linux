# Changelog

## 0.6.0 — standalone desktop plugins

- Extract Linux desktop plugins and the modified engine from the Codex fork.
- Preserve the marketplace identity and all four desktop companions.
- Integrate engine upstream through `ac7582d` (v0.7.1 plus rmcp 2.1), retaining
  the local capture, window ownership, observation, and transaction behavior.
- Add stock Codex compatibility checks and keep media results model-visible
  through the public MCP interface.
- Replace Codex sync maintenance with checked engine-update proposals and
  visible, deduplicated blocked-update reporting.
- Gate immutable prebuilt releases on backend, desktop, and client checks;
  provide verified release extraction and rollback instructions.

See `computer-use-linux/UPSTREAM.md` for the engine baseline and retained changes.
