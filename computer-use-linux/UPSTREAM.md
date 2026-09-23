# Upstream provenance and ownership

The vendored engine in `upstream/` tracks
[`agent-sh/computer-use-linux`](https://github.com/agent-sh/computer-use-linux).
Its integration baseline is recorded in [`UPSTREAM.toml`](UPSTREAM.toml).
Avi Fenesh created the original implementation. The engine and imported MIT components retain their MIT licenses. Companion
plugins and repository files retain their respective Apache-2.0 notices; see the
root NOTICE and individual manifests. This standalone repository does not
contain or build the Codex runtime. The former Codex fork is retained at
[`codex-computer-use-linux-legacy`](https://github.com/Gabriel-Kahen/codex-computer-use-linux-legacy).

## Integration policy

The revision is a **merge baseline**, not a claim that this directory is an
unmodified upstream release. Updates preserve the local patches below. The
update script performs a three-way merge against the exact pinned tree;
automation opens a review PR only after verification and reports conflicts
explicitly. Never advance the baseline merely because an update was attempted.
Generic fixes should be contributed upstream when practical.

The September 2026 integration incorporates upstream through
`ac7582dfcf907a204a9e1527da62d9ebb2676932` (v0.7.1 plus the rmcp 2.1 update).
This includes bounded AT-SPI discovery/traversal, deeper GTK4 trees, explicit
scope/truncation reporting, validated asynchronous ydotool probes, Unicode
wtype input, bounded command execution, typing deadlines, terminal paste
shortcuts, compositor focus/capture fixes, persisted accessibility setup
verification, the accessibility guard, and non-systemd/degraded installation
support. Dependency versions remain reproducible through `Cargo.lock`.

## Retained local patches

- **Window ownership and observations.** Shared claim leases, session identity,
  fencing, observation-bound element actions, compact/cached AT-SPI hydration,
  adaptive screenshots, and validated action batches remain local. Upstream's
  raw element-action helpers are not exposed as an alternative to observation
  validation.
- **Desktop transactions.** The local transaction boundary retains locks and
  ownership across complete input sequences when a tool request is cancelled.
  Upstream subprocess bounding and portal release cleanup are adapted to that
  boundary rather than adding a second competing input lock.
- **Unified portal session.** Pointer and keyboard permissions continue to share
  the local portal session and restore-token store. Input bus calls have bounded
  deadlines; failed/cancelled held keys and buttons trigger release cleanup and
  invalidate the session. Trusted stream geometry and the existing conservative
  screenshot-coordinate mapping remain authoritative. New upstream monitor
  helper APIs are retained for future integration, not advertised as a replacement
  for that mapping.
- **Native compositor integrations.** Inactive-window capture, Hyprland native
  batching, persistent COSMIC/KWin helpers, Niri, X11 capture workers and native
  IPC retain their local contracts. Hyprland capture scale matches the global
  compositor coordinate conversion.
- **Stock Codex image compatibility.** Image-bearing MCP results put metadata in
  text content and images in image blocks, without `structuredContent` or an
  advertised `outputSchema`. This is a valid MCP content-only result and avoids
  stock Codex ignoring image blocks when structured content is present. Text-only
  results may still use structured content. The batch/observe consumer accepts
  both representations. No Codex runtime patch is required.
- **Tool scope.** Upstream's optional generic shell-execution and completion
  notification tools are deliberately not exposed by this desktop engine: stock
  Codex already owns shell execution and task completion. The upstream Pi/npm
  distribution files are retained as provenance; this repository releases and
  tests its Codex plugins, not a separate Pi/npm package.

## Secondary patch feed

[`ilysenko/codex-desktop-linux`](https://github.com/ilysenko/codex-desktop-linux/tree/main/computer-use-linux)
is reviewed selectively. The initial backend came from
`b21a19ab9f9c142ee068b4f075f42710246a46f2`. Retained contributions include Plasma 5
compatibility (`0bc0272689166aa337c36bd2ad236477e523599e`), Niri
(`bdd5885953fa69e585a836e5ad8e4c666733eae7`), ydotool validation
(`197d8bad470d5e3bda0b96eabaebc7cb972e20d6`), GNOME screenshots/reload detection
(`eed9c0c8215655f73cf6313b0efdead81ec700ce`), and the Chrome integration from
`8494ee9ff73e233403ecf1fbf683c3e27bf99896`.

The secondary revision `89a11cd8d50a68f31ab45e34cd98a565498fb10b` was reviewed but
its desktop pet-cursor socket was not ported: it is product-specific UI feedback.
