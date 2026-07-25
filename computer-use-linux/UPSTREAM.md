# Upstream provenance and ownership

The generic engine under `upstream/` is based on
[`agent-sh/computer-use-linux`](https://github.com/agent-sh/computer-use-linux)
at commit `8cc1fafb78d9df047ca89a1974735c1a2bbc5060` (tree
`89108bc0d5123177c7d1a3b4f55f9e0a901ea25b`). Avi Fenesh created the original
implementation, with subsequent contributions from both the standalone and
`codex-desktop-linux` communities.

`agent-sh/computer-use-linux` is the primary source for generally useful Linux
desktop behavior. Generic fixes developed here should be contributed there
when practical, then consumed through the pinned update process.

[`ilysenko/codex-desktop-linux`](https://github.com/ilysenko/codex-desktop-linux/tree/main/computer-use-linux)
is a secondary patch and integration feed. The initial backend brought into
this repository came from its commit
`b21a19ab9f9c142ee068b4f075f42710246a46f2`. The consolidation also ports:

- Plasma 5 KWin compatibility from `0bc0272689166aa337c36bd2ad236477e523599e`;
- Niri support from `bdd5885953fa69e585a836e5ad8e4c666733eae7`;
- modern `ydotool` validation from `197d8bad470d5e3bda0b96eabaebc7cb972e20d6`;
- GNOME extension screenshots and reload detection from
  `eed9c0c8215655f73cf6313b0efdead81ec700ce`; and
- the Codex Chrome host/runtime through the desktop subtree change
  `8494ee9ff73e233403ecf1fbf683c3e27bf99896`.

The desktop subtree change
`89a11cd8d50a68f31ab45e34cd98a565498fb10b` was reviewed but not ported:
its Codex Desktop pet-cursor socket is product-specific UI feedback rather
than generic MCP backend behavior.

The machine-readable record is [`UPSTREAM.toml`](UPSTREAM.toml). The update
script three-way merges primary changes and never overwrites unresolved local
work. Automation may open a draft review PR after verification; it never merges
one. Desktop changes are reviewed and ported selectively because that subtree
also contains product-specific behavior that does not belong in the generic
engine.

The backend and retained integrations remain MIT licensed. The surrounding
Codex repository retains its existing license.
