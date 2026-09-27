#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname -- "$SCRIPT_DIR")
cargo build --locked --release --manifest-path "$REPO_DIR/rust/Cargo.toml"
exec "$REPO_DIR/rust/target/release/codex-monitor-rs" install "$@"
