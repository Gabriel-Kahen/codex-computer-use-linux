set positional-arguments

default:
    @just --list

computer-use-build:
    ./computer-use-linux/bin/codex-computer-use --build-only

computer-use-run *args:
    ./computer-use-linux/bin/codex-computer-use "$@"

computer-use-validate:
    python3 computer-use-linux/scripts/validate_plugin.py

computer-use-test:
    cargo test --locked --manifest-path computer-use-linux/upstream/Cargo.toml --all-targets

computer-use-chrome-test:
    cargo test --locked --manifest-path computer-use-linux/codex-integration/chrome-host/Cargo.toml

python-test:
    python3 scripts/test_python.py

fmt:
    cargo fmt --manifest-path computer-use-linux/upstream/Cargo.toml --all
    cargo fmt --manifest-path computer-use-linux/codex-integration/chrome-host/Cargo.toml --all

lint:
    cargo fmt --manifest-path computer-use-linux/upstream/Cargo.toml --all -- --check
    cargo clippy --locked --manifest-path computer-use-linux/upstream/Cargo.toml --all-targets -- -D warnings

check: computer-use-validate python-test lint computer-use-test computer-use-chrome-test

computer-use-upstream-status:
    python3 computer-use-linux/scripts/sync_upstream.py status

computer-use-upstream-prepare:
    python3 computer-use-linux/scripts/sync_upstream.py prepare

# Register this source checkout explicitly; regular users should use release bundles.
computer-use-install: computer-use-build
    codex plugin marketplace add "$PWD"
    codex plugin add computer-use-linux@codex-computer-use-linux
