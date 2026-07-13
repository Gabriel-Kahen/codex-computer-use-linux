# Linux Computer Use

This fork is based on the upstream Codex CLI. Linux Computer Use work belongs
in the CLI runtime rather than only in an external MCP server.

The Hyprland background-control project is preserved under
`contrib/hyprland-background-computer-use/` as the Linux/Wayland reference
implementation. It provides window capture, AT-SPI actions, Wayland and
XWayland input, and focus-preserving fallbacks.

Planned native integration:

1. Define Linux Computer Use tools in the Codex core tool registry.
2. Reuse the Hyprland backends behind a native tool interface.
3. Replace the generic observe/action cycle with a Linux-aware controller.
4. Add approvals, desktop leases, interruption, recovery, and parallel-worker
   arbitration in the Codex turn loop.

Keep `codex-upstream` as the upstream Codex remote and
`hyprland-upstream` as the source for the contributed Hyprland integration.
