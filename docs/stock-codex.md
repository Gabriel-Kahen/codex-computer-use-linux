# Stock Codex compatibility

This repository ships desktop plugins and a modified Linux engine, not a Codex
runtime. `scripts/stock-codex/package.json` records the supported stock CLI baseline (currently
`0.156.1`). The compatibility workflow also resolves the latest official stable
npm release once, installs that exact version, and tests it. Both jobs gate
releases and upstream update proposals. A weekly run detects Codex regressions
without waiting for an engine change.

The pinned test uses the official `@openai/codex` package, including npm integrity
hashes in `scripts/stock-codex/package-lock.json`. It does not trust an arbitrary
`codex` executable already installed on a runner. The latest job records its
resolved version in the uploaded compatibility report; it does not silently
change the supported baseline.

## What is tested

`scripts/stock_codex_compat.py` launches the official app-server with an isolated
configuration and no account credentials. It:

1. Assembles a temporary release-layout marketplace with the built engine and
   helper binaries, installs all five actual plugins through official Codex CLI,
   and initializes app-server and a temporary thread.
2. Discovers all five installed MCP servers and their tools, then
   invokes the read-only `doctor` tool. A headless runner can report unavailable
   desktop capabilities; it must still return a valid diagnostics report.
3. Calls a synthetic MCP image tool through app-server and checks exact image
   bytes and coordinate metadata.
4. Uses a loopback-only, deterministic Responses fixture to request the same
   tool, then inspects the next model-bound request. Both the image and its
   metadata must survive Codex's tool-result serialization.
5. Verifies host-supplied `_meta.threadId` on both direct and model-driven calls,
   so window ownership does not depend on a fork-only runtime patch.
6. Probes the old mixed `structuredContent`/image shape separately and reports
   whether that Codex version supports it.

There is no hosted inference, authentication, real desktop screenshot, or desktop
input in this test. The real-compositor Hyprland job separately tests capture and
input. The synthetic test establishes transport and model-context compatibility;
it does not establish how well a model understands a screenshot or how the Codex
desktop app renders one.

## Why the media result shape changed

Official Codex `0.156.1` preserves both fields in a direct app-server MCP response
but selects `structuredContent` when building model context, losing accompanying
image blocks. This was reproduced with the official npm binary, not inferred
from the former fork's patches.

The engine therefore sends media-bearing results as MCP `content` containing
JSON metadata text and image/audio blocks, with no `structuredContent`. Text-only
results can continue to include `structuredContent`. Companion screenshot
results already follow the compatible content-only form. Metadata such as image
dimensions and coordinate transforms stays available to the model.

The old Codex runtime patches are retained in the archived fork. We do not claim
that every patch is upstream or universally unnecessary. For this project's
screenshot path, the compatible MCP response removes the dependency on the
runtime media patch. Engine-side output limits remain the plugin's responsibility.

## Run locally

```sh
npm ci --prefix scripts/stock-codex --ignore-scripts --no-audit --no-fund
computer-use-linux/bin/codex-computer-use --build-only
python3 scripts/stock_codex_compat.py \
  --codex "$PWD/scripts/stock-codex/node_modules/.bin/codex"
```

To test another official version, install that exact version separately and pass
its binary with `--codex` and its version with `--expected-version`. A failed
image or metadata assertion must block release; never turn it into an allowed
failure to make a new Codex version appear supported.

The app-server transport and initialization follow
[official OpenAI documentation](https://developers.openai.com/codex/app-server).
App-server APIs evolve, so keep this integration test alongside the engine tests.
