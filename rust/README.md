# Canonical Rust runtime

`rust/` is the authoritative implementation of Codex Monitor. The installed command
is `codex-monitor`; the source build target retains `codex-monitor-rs` for compatibility.

See [installation, upgrades and Python-state migration](../docs/INSTALLATION.md),
[dashboard controls](../docs/DASHBOARD.md), and [owner lifecycle](../docs/OWNER-LIFECYCLE.md).
Historical comparative measurements remain in [Rust evaluation](../docs/RUST-EVALUATION.md).

```bash
cargo build --locked --release --manifest-path rust/Cargo.toml
cargo test --locked --manifest-path rust/Cargo.toml
cargo clippy --locked --manifest-path rust/Cargo.toml --all-targets -- -D warnings
./scripts/install.sh --with-skill
```

Receiver, dashboard, resident, sampler, permissions/reconnect, migration and native
installation run in Rust. Python is used only by optional integration helpers,
legacy recovery tools and test harnesses. New runtime behavior belongs in Rust;
Python sources are not a parallel product implementation.
