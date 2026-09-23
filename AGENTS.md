# Linux desktop plugins

This is a standalone plugin project, not a Codex runtime fork. Keep desktop
features usable through public MCP and stock Codex interfaces. Do not vendor
the Codex CLI, app server, or desktop frontend.

- Preserve upstream attribution and the modified engine under
  `computer-use-linux/upstream/`. Never replace it wholesale during an update.
- Change `UPSTREAM.toml` only after actually integrating that exact revision.
- Keep plugin identifiers and marketplace identity compatible with existing installs.
- Use `just check` for the engine, packaging, and companion unit tests. Native
  compositor tests run in isolated sessions/CI, never on the user's live apps.
- Every input/capture change needs a relevant behavioral test. Do not infer
  live desktop correctness from mocked tests alone.
- Releases must pass CI, stock Codex compatibility, and native Hyprland checks.
  Publish immutable checksummed artifacts with provenance. Do not bypass gates.
- Keep blocked upstream updates visible; never report a suppressed failure as healthy.
- Do not change the user's installed plugins or desktop configuration as part
  of repository development unless installation is requested.
