# Linux service reconnect

The Rust dashboard supports **Reconnect** for direct local Codex owners and
residents managed by `systemd --user`. macOS continues to use LaunchAgents.
Linux requires `systemctl`, a running user service manager, and readable `/proc`
process information. System services, other supervisors and shell wrappers are
not discovered by this action.

## Service configuration

Place regular, user-owned `.service` files in
`$XDG_CONFIG_HOME/systemd/user`, or `~/.config/systemd/user` when that variable
is unset. Files must not be writable by group or others. Use a direct absolute
executable, `Type=exec` or `Type=simple`, and literal arguments. The saved unit
must match the loaded service and its running process. Aliases, templates,
execution-changing drop-ins, pending daemon reloads, environment files, command expansions,
line continuations and additional execution hooks are rejected. This deliberately
limited format avoids restarting a service whose effective configuration cannot
be verified. Quoted arguments (including quotes within an argument), repeated `Environment=`
lines, `UMask` and resource limits are supported. Drop-ins containing only
`MemoryHigh`, `MemoryMax`, `TasksMax` or `CPUQuota` are accepted after ownership
and content checks. An unset `CODEX_HOME` is equivalent to `HOME/.codex`.

For example, an owner unit named `codex-owner.service`:

```ini
[Unit]
Description=Local Codex owner

[Service]
Type=exec
ExecStart=/absolute/bin/codex app-server --listen ws://127.0.0.1:4500
Environment=HOME=/absolute/home CODEX_HOME=/absolute/home/.codex
WorkingDirectory=/absolute/project
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

A matching resident unit named `codex-resident.service`:

```ini
[Unit]
Description=Codex conversation resident
After=codex-owner.service

[Service]
Type=exec
ExecStart=/absolute/bin/codex-monitor resident --endpoint ws://127.0.0.1:4500 --thread THREAD_ID
Environment=HOME=/absolute/home CODEX_HOME=/absolute/home/.codex
WorkingDirectory=/absolute/project
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

Replace the paths and `THREAD_ID` with your installed executables, existing
Codex home, project and CLI-owned conversation. Preserve the conversation's
chosen permissions and any required resident arguments. Each loaded conversation
must have resident coverage; repeated `--thread` arguments are supported. Run
the dashboard with the same `HOME` and `CODEX_HOME` as these services.

After saving the units, load and start them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codex-owner.service codex-resident.service
systemctl --user status codex-owner.service codex-resident.service
codex-monitor dashboard
```

Select the conversation and use Reconnect, then confirm its preview. The action
verifies the current saved login and idle conversations, rechecks the service
configuration, and requests one restart of the exact owner unit. Residents
remain running and restore their subscriptions. Completion requires a changed
owner PID, the verified account, account access and restored loaded conversations.
No failed input is replayed. A restart timeout is reconciled without issuing a
second restart.

The dashboard supports explicit **Project permissions** or **Server permissions**
changes. Both affect the shared owner, not just the selected conversation. The
project label is used only when every configured conversation maps to one project;
the menu lists all affected names and the confirmation also lists thread IDs.
Saved labels inherit explicit owner sandbox options when a resident has no override.
They do not verify the effective policy of an already attached client.

All loaded conversations must be idle or in an execution-error state, and all
covered queues must be empty. Resolve pending approvals or questions in Codex
before applying a change. Confirmation backs up units, changes only ExecStart,
reloads systemd, restarts the owner and residents, and checks returned sandbox
modes for all configured conversations. Failure restores the saved files and
reports whether service restoration completed; inspect effective state before
retrying. Full Access preserves approval policy and does not grant OS sudo
privileges. Project Access preserves configured additional writable roots.

The receiver
`service` installer remains macOS-specific; Linux receiver supervision is
configured separately as described in [operations](OPERATIONS.md).

## Verification scope

Local regression tests cover unit discovery, quoting, unsafe file exclusion,
loaded-unit identity, stale configurations, process arguments, login environment
and working-directory mismatches. A separate opt-in test uses a disposable
`/usr/bin/sleep` user service to verify real manager restart, PID replacement,
unit rewriting, stop/start and restoration of original arguments:

```bash
cargo test --locked --manifest-path rust/Cargo.toml
cargo test --locked --manifest-path rust/Cargo.toml \
  real_user_manager_restarts_only_disposable_service -- --ignored
cargo clippy --locked --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

The opt-in test requires a Linux user manager and `XDG_RUNTIME_DIR`; it creates
and removes only its uniquely named runtime unit and reloads the manager's unit
configuration. These checks do not establish real Codex conversation restoration,
model execution or an external reply.
