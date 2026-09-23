# Contributing to Codex Computer Use on Linux

Contributions that improve Linux computer use, desktop compatibility, safety,
reliability, documentation, or the integration with this Codex fork are
welcome.

This is an independently maintained fork. Pull requests intended for this
project should use
[`Gabriel-Kahen/codex-computer-use-linux`](https://github.com/Gabriel-Kahen/codex-computer-use-linux),
not OpenAI's contributor channels.

## Before you start

- GitHub Issues and Discussions are currently disabled. The project does not
  presently offer a general support or feature-request channel.
- For a large feature, new desktop backend, or broad Codex runtime change, open
  a draft pull request with a concrete design and clearly mark it as a proposal
  before investing in the complete implementation.
- Use [SECURITY.md](SECURITY.md) for a suspected vulnerability. Do not disclose
  security-sensitive details in a public pull request or other public channel.
- Send a bug that occurs only in unmodified OpenAI Codex to the
  [upstream project](https://github.com/openai/codex/issues). If it affects this
  fork or its Linux integrations, a pull request fixing it is in scope here.

## Project layout

The main development areas are:

| Path                           | Purpose                                                                   |
| ------------------------------ | ------------------------------------------------------------------------- |
| `codex-rs/`                    | Forked Codex CLI, TUI, app server, agent runtime, and plugin host         |
| `computer-use-linux/upstream/` | Provenance-pinned Linux computer-use engine                               |
| `computer-use-linux/`          | Plugin packaging, launch, identity, update policy, and Chrome integration |
| `contrib/`                     | Hyprland, GNOME, Plasma, and X11 desktop integrations                     |

The generic engine is synchronized through the process documented in
[`computer-use-linux/UPSTREAM.md`](computer-use-linux/UPSTREAM.md). Do not
replace imported sources or remove upstream attribution manually.

## Development setup

Install a current Rust toolchain, Cargo, and `just`, then fetch the Codex
workspace dependencies:

```shell
just install
cargo install --locked cargo-nextest
```

Build the forked CLI with:

```shell
cargo build --manifest-path codex-rs/Cargo.toml -p codex-cli --bin codex
```

Build and inspect the Linux backend with:

```shell
just computer-use-build
just computer-use-run doctor
```

Desktop-specific system packages are listed in the README under each
integration in `contrib/`.

## Making changes

- Follow the nearest `AGENTS.md` instructions for every file you touch.
- Keep changes focused and preserve the separation between the Codex workspace,
  the shared Linux engine, and compositor-specific integrations.
- Treat capture, accessibility data, focus, keyboard input, pointer input,
  window claims, and state restoration as security-sensitive behavior.
- Add or update tests for behavioral changes. User-visible Codex TUI changes
  also require the snapshot coverage described in `AGENTS.md`.
- Retain third-party copyright, license, trademark, and provenance notices.

## Validation

Run the checks relevant to the area you changed.

For the Linux engine and plugin:

```shell
just computer-use-test
just computer-use-validate
```

For the optional Chrome host:

```shell
just computer-use-chrome-test
```

For a changed Codex Rust crate, format the workspace and run its focused test
suite from `codex-rs/`:

```shell
just fmt
just test -p <package>
```

The repository's GitHub Actions workflows run additional platform and
integration checks. A pull request should explain any check that could not be
run locally.

## Pull requests

Open pull requests against `main` and include:

- the problem and the reason for the chosen approach;
- a linked issue when one exists;
- the user-visible and safety impact;
- tests performed and their results; and
- screenshots or terminal output when they make the behavior easier to review.

Keep unrelated changes in separate pull requests. Maintainers may ask for a
change to be narrowed, reworked, or moved upstream. Acceptance is not
guaranteed, even when a contribution is well implemented.

Unless explicitly stated otherwise, contributions submitted for inclusion in
this repository are licensed under the [Apache License 2.0](LICENSE), as
described in section 5 of that license.
