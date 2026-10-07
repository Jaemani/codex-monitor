# Installation and migration

Rust is the canonical runtime. Build requirements are Rust/Cargo 1.97.1 or newer
and a C compiler for bundled SQLite. The installed executable needs no Python
interpreter. Codex CLI is required for owner connections. No registry release is
assumed; use a trusted checkout or a verified native executable.

## Fresh installation

```bash
./scripts/install.sh --with-skill
codex-monitor --version
codex-monitor init
```

On macOS, register and start the receiver LaunchAgent:

```bash
codex-monitor service install
```

On Linux, run it in the foreground or configure an OS supervisor:

```bash
codex-monitor serve
```

Then open a separate terminal:

```bash
codex-monitor dashboard
```

The executable is registered at `~/.local/bin/codex-monitor`, pointing into
`~/.local/share/codex-monitor-rust/current/bin/codex-monitor`. Add `~/.local/bin`
to PATH if necessary. Native releases are immutable and identified by SHA-256.
`--prefix` and `--bin-dir` select absolute custom locations. `--with-skill` installs
the bundled skill; it does not create a conversation, route, or external resource.
The optional skill helper script uses Python only when invoked; daemon, resident,
dashboard, storage, migration and service operations are native Rust.

The default state directory remains `~/.local/state/codex-monitor` and honors
`CODEX_MONITOR_HOME`. `CODEX_MONITOR_RUST_HOME` remains a fallback for earlier isolated
Rust installations. Existing state is never silently initialized or migrated.

`service` supports macOS user LaunchAgents. On Linux, run `codex-monitor serve`
under the chosen supervisor; the native macOS service command does not claim Linux
supervision support. Dashboard reconnect is a separate operation and supports
[Linux systemd user owner/resident services](LINUX-RECONNECT.md).

## Adopt an existing Python installation

Keep the old executable path and service definitions for rollback. Build first,
then stop the receiver. Do not run both receivers against the same endpoint.

```bash
cargo build --locked --release --manifest-path rust/Cargo.toml
codex-monitor service stop
rust/target/release/codex-monitor-rs migrate
rust/target/release/codex-monitor-rs install --adopt-python --with-skill
codex-monitor service install
codex-monitor dashboard --once --json
```

Migration requires the receiver lock to be free. It snapshots SQLite databases,
including committed WAL pages, into a private migration backup; preserves binding,
event, native client, reply and request identifiers; copies watch baselines and
keeps credentials unchanged. The new database is `rust.sqlite3`; the original
Python databases and configuration backup remain available. A pending uncommitted
watch event blocks adoption until reconciled. Custom legacy limit overrides also
block adoption until explicitly mapped; they are not silently discarded. No model turn or failed input is
replayed by migration. Normal receiver dispatch resumes pending input afterward.

The owned legacy `~/.local/share/codex-monitor/bin/codex-monitor` entry point also
forwards to Rust after adoption; immutable historical releases remain available
for deliberate recovery.

Resident services must also be restarted with the native executable, retaining
their exact endpoint, thread list, environment and permission flags. Coordinate
this at an idle boundary. Receiver replacement alone does not update an already
running resident. Owner servers and account credentials need not change.

Rollback is safe only before Rust accepts new work: stop Rust, restore the saved
configuration and executable/service definitions, then restart the old receiver.
Once Rust has accepted events or replies, do not simply switch back to the old
Python database; reconcile those newer records first to prevent loss or duplication.

## Upgrade Rust

Build a release, stop the receiver, install the native release, and reinstall the
receiver service to record its new immutable executable. Restart residents at an
idle boundary. Existing state, credentials, permission flags and external routes
are preserved. The native installer does not silently restart services.

`python3 scripts/install.py install` is a compatibility entry point to the Rust
build/install path. Wheel-based Python installation requires an explicit
`--legacy-python` flag and is retained only for historical recovery.

## Native archives

`python3 scripts/build-release.py --out dist` packages the host-native Rust
executable, embedded skill and reviewed installation instructions with SHA-256
checksums. Python 3.11+ is a packaging-tool dependency only. This command does not
publish an archive. After verifying and extracting a matching host archive, run
`./bin/codex-monitor install --with-skill`. Archives contain no Python wheel.

## Existing processes after an upgrade

Changing the installed command does not replace code already running in a terminal.
Close an idle legacy dashboard through its normal quit control and launch
`codex-monitor dashboard` again. If it currently hosts a Codex TUI, preserve that
client until the user returns with `/quit`; do not kill its parent or process group.
A remote terminal also requires coordination with its owner before restarting its
view. These interactive processes are distinct from supervised receiver/resident
services. Audit actual executable paths, not just the current symlink.

Discord producer adoption has a separate database and service boundary; follow
[the native Gateway cutover procedure](DISCORD-GATEWAY.md). Keep unrelated Python
applications and one-shot project reply adapters outside the runtime cutover.
