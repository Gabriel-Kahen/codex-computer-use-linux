# Codex Chrome extension host

This crate preserves the Codex-specific Linux native-messaging bridge and its
bounded app-server runtime. It is intentionally separate from the generic
`agent-sh/computer-use-linux` backend and is not started by the Computer Use MCP
plugin.

Build and test it independently:

```shell
just computer-use-chrome-test
```

The recipe keeps Cargo artifacts in the same external cache hierarchy as the
MCP backend so plugin installation does not copy them.

The source is synchronized selectively from the
`codex-desktop-linux/computer-use-linux` integration rather than from the
generic backend upstream.
