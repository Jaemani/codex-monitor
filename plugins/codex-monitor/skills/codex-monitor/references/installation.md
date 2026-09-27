# Installation

The canonical runtime is native Rust. Use a trusted checkout of
https://github.com/Jaemani/codex-monitor and run `./scripts/install.sh --with-skill`.
Building requires Rust/Cargo 1.97.1+ and a C compiler; installed runtime commands
need no Python interpreter. The optional context helper uses Python.

Default command: `~/.local/bin/codex-monitor`. Its native release is under
`~/.local/share/codex-monitor-rust/current/bin/codex-monitor`. Add the command
directory to PATH or set CODEX_MONITOR_BIN to an absolute executable path.

For existing Python state, use the repository's docs/INSTALLATION.md migration
procedure: stop the receiver, run native migrate, install with --adopt-python,
and reinstall the receiver. Preserve credentials, routes and original databases.
Restart resident services with the native executable at an idle boundary, retaining
endpoints, threads and permissions. Never run competing receivers or reset state.

The default state stays ~/.local/state/codex-monitor. Runtime status reports rust.
Native service management supports macOS LaunchAgents; Linux requires an external
supervisor. Installing a skill does not attach conversations or start producers.
