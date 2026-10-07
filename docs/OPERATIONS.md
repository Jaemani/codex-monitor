# Operations and recovery

## State and permissions

The `--state` directory contains `config.json`, admin and source token files,
`rust.sqlite3` and its WAL files, `serve.lock`, and persisted watch checkpoints.
Legacy Python databases are retained only as migration evidence. The
directory is created with mode `0700`; config and token files use `0600`. Do
not add this directory to a model's writable root. Running processes as the
same OS user is not a security boundary, and full filesystem access can still
modify operational files.

`status`, event IDs, and `inspect` read local state and do not call the model.
HTTP status endpoints require the admin credential; a source credential cannot
read another source's events or state.

The examples below assume the installed `codex-monitor` launcher is available
on `PATH`. The installer does not edit `PATH`; if it prints an absolute
launcher path, use that path instead.

| State | Meaning |
|---|---|
| `pending` | Not yet delivered, or waiting for a safe retry. |
| `submitting` | Persisted before the call; reconcile it after a process crash. |
| `accepted` | Delivery was confirmed by a queue response or matching client ID in queue/history. |
| `uncertain` | Delivery is not known; later events for the same binding are held until it is resolved. |
| `dead` | Explicit rejection, retry limit, or expiry; operator action is required. |
| `ignored` | Recorded as `agent.ack` without waking the model. |
| `discarded` | Discarded after operator review. |

`accepted` does not mean that the model completed the task. Queue deletion,
cancellation, model failure, or approval waiting are not task success. Check
the conversation and the surrounding system of record for the outcome.

To compare the local receipt with Codex queue/history, run the read-only
inspection:

```bash
codex-monitor inspect "$DELIVERY_ID"
```

`local.state` is the monitor database state. `native.state=queued` means the
client message ID is currently in the native queue; `consumed` means it is in
thread history; `unknown` means the bounded lookup found no current evidence
or could not complete. `unknown` is not proof of non-delivery.

Inspection does not change local state and does not delete the queue or call
`thread/start`, `thread/resume`, or `turn/start`. It also does not interrupt a
turn. The API has no direct post-cancellation queue status, so `queued` is not
automatically treated as `paused`. A native `consumed` result does not resolve
a local `uncertain` state automatically; an operator must review the result.

## Retry and reconciliation

Pre-delivery connection failures use exponential backoff (`1, 2, 4, ...`, up
to 60 seconds), with five attempts by default and a 3,600-second age limit.
FIFO follows local receipt order; upstream timestamps and causal order do not
reorder events. A failure in one binding does not block another.

If a queue call loses its connection or response, the monitor does not blindly
retry. It searches for the same `clientUserMessageId` in the queue and recent
history. If it is not found, delivery remains `uncertain`. Reconciliation
combines queue and history scans, bounded to 100 pages and 30 seconds, and
passes the remaining deadline to each RPC. Exhausting either bound leaves the
event uncertain. Old or compacted history and user deletion are part of this
boundary. Uncertain checks run every 30 seconds without waking the model.

Stop `serve` with Ctrl-C or the service manager, review the cause, then record
an explicit decision:

```bash
codex-monitor resolve "$DELIVERY_ID" --as accept --reason 'Confirmed in the conversation'
codex-monitor resolve "$DELIVERY_ID" --as discard --reason 'Cancelled by the operator'
codex-monitor resolve "$DELIVERY_ID" --as replay --reason 'Confirmed not delivered'
```

Use `replay` only after assessing duplicate risk. It keeps the same client ID
and records the decision. `resolve` is rejected while `serve` is running and
cannot arbitrarily reset normal `accepted` or `pending` events. Recheck the
binding and incoming events before restarting `serve`.

## Limits and retention

The native receiver uses fixed limits: five delivery attempts, a 3,600-second
age limit, 1,000 unresolved events per binding, 120 new events per binding per
minute, and 16 events per trace. The hop limit is eight. Duplicate intake does
not consume new-event quota. Legacy `config.json` limit overrides are not applied;
migration refuses nonempty custom limits instead of silently ignoring them.

Records are not deleted automatically. Deleting old records also removes
deduplication evidence, so define a retention policy and monitor state volume.
Stop `serve` and other state-writing CLI commands before backing up the entire
state directory. Preserve all databases, WAL files and checkpoints together.

## Tracked requests

`request track` binds an original external receipt to a conversation and source.
Only explicit CLI or source-authenticated HTTP updates change its work state;
native delivery and reply acknowledgement do not complete requests. The receiver
records expiries and drains an ordered notification outbox. A paused binding
defers its notifications without blocking other conversations.

If notification intake fails, `request status` retains the pending notification,
error, retry time and original route. Once locally accepted, its `delivery_id`
can be checked with `inspect`; acceptance is not proof of native consumption.
Request-pump errors remain visible in receiver health while ordinary event
dispatch continues. The default request store retains 10,000 requests, with
terminal notification capacity reserved for open requests. There is no automatic
pruning. See [request lifecycle](REQUEST-LIFECYCLE.md) for commands and limits.

## Managed collectors

`monitor create` stores a conversation-scoped file watch; `serve` owns its sampling and checkpoint recovery. No external source token is needed. Use `monitor status NAME` to distinguish configured state, receiver liveness, observed sampling, errors and receipts. Pause or remove one monitor without stopping other conversations. External producers and legacy `watch-file` processes remain independently operated. See [conversation monitors](CONVERSATION-MONITORS.md) for scope, limits and recovery semantics.

When running the receiver in Docker, use `--init` or an equivalent init/reaper.
After receiver SIGKILL, terminated sampler children can otherwise remain as
zombies owned by a non-reaping PID 1. The Linux failure/recovery canary verifies
reaping separately from worker termination.

## Long-running service

The default `shared-local` path needs only `serve`. Keep `host` separately
running only when a direct WebSocket connection is required. Run the same
absolute command under the OS service manager; this is a systemd example:

```ini
[Unit]
Description=Codex Monitor event receiver
[Service]
Type=simple
ExecStart=/absolute/bin/codex-monitor --state /absolute/state serve
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
```

The dashboard can reconnect a Linux owner managed by `systemd --user`; see
[Linux service reconnect](LINUX-RECONNECT.md) for owner/resident units and validation
requirements. Receiver supervision and owner reconnect are separate operations.

On macOS, install the immutable native release, then register its receiver
LaunchAgent using the `service` command:

```bash
/absolute/bin/codex-monitor --state /absolute/state service install
/absolute/bin/codex-monitor --state /absolute/state service status
/absolute/bin/codex-monitor --state /absolute/state service restart
/absolute/bin/codex-monitor --state /absolute/state service stop
/absolute/bin/codex-monitor --state /absolute/state service start
/absolute/bin/codex-monitor --state /absolute/state service uninstall
```

`install` registers and starts a per-state user LaunchAgent. `start` is
idempotent; `restart` replaces it; `stop` unloads the job but keeps its plist;
and `uninstall` removes only that job and plist while preserving config, tokens,
and the event database. Logs are private files under `state/service`.
`status.loaded` reports launchd registration, not HTTP health or model
completion.

Pin `CODEX_HOME` and any explicit `CODEX_SQLITE_HOME` in the service so it
continues to use the same Codex store. For remote authentication, set
`CODEX_MONITOR_SERVER_TOKEN_FILE` to an absolute, owner-readable credential
file; the plist stores the path, not the credential. The direct
`CODEX_MONITOR_SERVER_TOKEN` environment value is suitable for foreground
execution but should not be copied into a service plist.

Native installation and macOS receiver/resident adoption have been verified.
Disposable fake-owner tests cover forced termination, recovery and duplicate
suppression. These checks do not prove Discord delivery or completed model work.

## Linux placement and supervision

The canonical Rust runtime is the Linux candidate. Use native systemd user
services first: the receiver, owner and resident share local Codex state, Unix
sockets and workspace permissions. No runtime dependency on Docker socket,
Compose, Buildx, privileged containers, Xcode, Keychain, Metal or MLX was found.
SQLite is bundled; TLS uses rustls. Build natively for each CPU architecture with
Rust/Cargo 1.97.1+ and a C compiler. Do not copy a macOS executable to Linux.
Node and Flutter are not monitor runtime dependencies. Python 3.11+ and
`websockets>=15,<17` are required only for the optional outage test below.

| Component | Placement classification | Evidence and remaining boundary |
| --- | --- | --- |
| Rust receiver, storage, managed file collectors | Linux development/operation possible | Linux tests and isolated receiver checks; rewrite host-specific watched paths and check case/permissions before migration |
| Terminal dashboard | Linux development possible; interactive acceptance pending | Unix terminal code builds; no Linux interactive dashboard acceptance claimed |
| CLI owner and resident | Linux development/operation possible with project acceptance | Isolated actual Codex 0.157.1 TUI consumption and owner restart passed; production account/workspace permissions still need acceptance |
| Native Discord Gateway and project reply adapters | Linux after project configuration and validation | Rust Gateway builds; Mac producers remain active; paths, credentials, receipts, attachments and external replies require separate acceptance |
| `service` and service reconnect controls | Mac retained; Linux requires implementation changes | `service.rs` and `reconnect.rs` depend on launchd; use external systemd for the Linux receiver only |
| Desktop, GUI-dependent agent tools | Mac retained | Linux receiver cannot transfer Desktop ownership or supply macOS GUI tools |
| Python implementation and benchmark drivers | Development/migration fixtures | Frozen reference; not an alternative production runtime |
| Website, database hosting, public tunnel | Outside this repository's deployment | No project-owned cloud deployment identified; do not change another project's infrastructure |
| Unused independent watcher/dashboard processes | Removal candidate only after ownership review | Process existence alone does not establish redundancy; preserve active conversations |

Podman/Quadlet and Docker Compose are not selected for this pilot. Containers
would add socket, UID, workspace mount and state-store mapping requirements
without removing a monitor dependency. Podman installation and rootless network,
volume ownership, health and reboot behavior have not been validated. This is
not a claim that containers cannot work. Same-user processes are not mutually
isolated; a dedicated user requires its own authenticated Codex environment and
explicit filesystem access.

### Isolated receiver pilot

The [example user unit](../examples/systemd/codex-monitor-pilot.service) is for
new empty state, not the existing receiver. Review the port with `ss -ltn` and
choose an unused nonprivileged port. Install the native executable using
[installation instructions](INSTALLATION.md). For development acceptance the
unit can instead pin the absolute release-build executable from the independent
checkout, avoiding changes to an installed command.

```bash
codex-monitor --state "$HOME/.local/state/codex-monitor-pilot" init --port 18766
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 examples/systemd/codex-monitor-pilot.service "$HOME/.config/systemd/user/"
systemd-analyze --user verify "$HOME/.config/systemd/user/codex-monitor-pilot.service"
systemctl --user daemon-reload
systemctl --user start codex-monitor-pilot.service
codex-monitor --state "$HOME/.local/state/codex-monitor-pilot" status
systemctl --user show codex-monitor-pilot.service -p MainPID -p MemoryCurrent -p Result
journalctl --user -u codex-monitor-pilot.service -n 50 --no-pager
systemctl --user stop codex-monitor-pilot.service
```

Initialize only a new state directory. Do not add real bindings or enable a
Gateway for this pilot. Check authenticated `receiver.ready` through `status`;
`active` alone is not health and `consumer_ready` is unknown for empty state.
The receiver handles SIGTERM; systemd bounds shutdown at 30 seconds and kills
remaining children as one control group. Crash restart is limited to five
starts per five minutes. Memory and CPU values are initial pilot ceilings,
not measured production capacity. Watch the whole owner/resident/receiver stack
before setting production budgets.

No owner ordering is needed for an empty receiver. A production owner and
resident need separate reviewed units and private endpoint configuration;
startup ordering is not a substitute for readiness or restored subscriptions.
Use the [Linux reconnect backend](LINUX-RECONNECT.md) for verified direct
owner/resident units; never infer ownership from a service name alone.

The optional private `~/.config/codex-monitor/pilot.env` must have mode 0600
under a 0700 directory. Set `CODEX_HOME`, `CODEX_SQLITE_HOME` if required, and
`CODEX_MONITOR_SERVER_TOKEN_FILE` only for the intended owner. The latter names
a private credential file; never put a bearer token in a unit or Git. Check the
unit's PATH resolves the intended `codex` command. Do not copy Mac account
stores, session history or authentication tokens to establish a Linux owner.

After separate boot-start approval, `systemctl --user enable
codex-monitor-pilot.service` selects login startup; an administrator can enable
lingering with `loginctl enable-linger USER` for operation without a login.
Inspect `loginctl show-user USER -p Linger` first. Reboot and verify actual
health before claiming unattended startup. The pilot is not enabled by this
procedure. The example uses per-unit journal rate limits; disk retention is the
host journal policy, not a per-unit byte cap. Coordinate journal/storage limits
with the infrastructure owner rather than editing global settings here.

See the installed `systemd.service(5)`, `systemd.exec(5)` and
`systemd.resource-control(5)` manuals for the host's actual version.

### Backup, update and rollback boundary

Monitor records have no automatic retention. Track database, WAL, attachments,
logs, Cargo targets and available disk separately; the build cache can greatly
exceed the receiver state. Do not prune deduplication records as generic cache.
Keep backups outside the checkout, encrypted and access-restricted, with a
separate-host copy and a reviewed retention policy.

At an approved idle boundary, stop the selected producers, receiver and other
state writers, then copy the complete private state directory with permissions,
including WAL/checkpoints, plus each Gateway database/config and executable/unit
versions. Never stop another project's owner as part of this step. Restore into
a private isolated directory first; run SQLite `PRAGMA integrity_check`, compare
receipt/request counts and identifiers, and keep all external routes disabled.
A valid SQLite file alone does not establish application recovery.

Build and test a new release in an independent checkout. Pin its executable in
the reviewed unit, preserve the previous executable and state snapshot, reload
systemd, then restart only the approved service and check readiness, receipts,
consumer behavior and actual source replies. Before any new writes, rollback can
restore the old executable/configuration and consistent state snapshot. After
new writes, preserve both histories and reconcile event IDs, uncertain sends,
outbox acknowledgements and Gateway checkpoints before resuming one writer.
Do not blindly overwrite the new database with an old copy.

Production acceptance still requires one new Linux CLI owner, verified login
and permission scope, actual event consumption and reply, owner outage recovery,
restore/reboot acceptance, and a project-specific single-writer cutover plan.
Keep Mac production running until those gates and cutover authorization exist.

## Endpoints and security boundaries

`shared-local` is an independent stdio writer using the official queue in the
same Codex store. It uses the same OS user, `CODEX_HOME`, and configured
`sqlite_home`, but does not load a conversation. The native Codex consumer
checks the external queue about every 10 seconds without calling the model.
The writer does not call `thread/start`, `thread/resume`, `turn/start`, or an
interrupt operation.

`local` uses the installed CLI's local daemon proxy; `ssh://ALIAS` uses an
existing SSH host; `ws://loopback` and `wss://` use standard WebSockets.
Remote bearer tokens may come from `CODEX_MONITOR_SERVER_TOKEN` or the private
file path in `CODEX_MONITOR_SERVER_TOKEN_FILE`; an environment token wins when
both are set. Do not expose an App Server without authentication. HTTP ingress
is loopback-only; remote webhooks need an existing TLS reverse proxy or SSH
tunnel deployment.

Direct server adapters require the target thread to be loaded on that server.
With `shared-local`, the official queue can accept an event while the UI is
closed, and the event may be consumed when the user reopens the conversation.
`max_age` applies before native queue acceptance; it does not delete an already
queued native message. Events are not moved automatically to another thread,
worker, store, or host.

## HTTP overload

The default concurrent request limit is 32. Excess connections receive
`503` and `Retry-After: 1` and are closed; senders should retry with the same
event ID. Events rejected before receipt are not stored. Socket inactivity
times out after 5 seconds. Python Server's `max_connections` can adjust the
connection limit, but it is not a total request deadline; public deployments
should set that deadline in the external reverse proxy.

## Pending approvals and user responses

For explicit local owner connections, the dashboard shows **Approval needed**
when Codex reports `waitingOnApproval` and **Response needed** when it reports
`waitingOnUserInput`. Open the conversation in its owning Codex client to review
the requested action or answer the question. Monitor does not approve `sudo`,
choose answers or respond to feedback requests automatically.

`codex-monitor dashboard --once --json` exposes these observations under each
binding's `owner_health.attention`. `status` is `required`, `none` or `unverified`;
recognized active flags include separate `approval_required` and
`user_input_required` booleans. Transport reachability is separate from a
conversation waiting for a human decision. Normal snapshots refresh the state;
this does not send a Discord notification or create an unattended alert.

Detection depends on the owner exposing the current flags. An ordinary prose
question, a terminal password prompt, an unsupported owner, or a request managed
outside that owner may not be detectable. Do not treat an absent flag as proof
that every possible wait has been ruled out.

## Session state-change notifications

The live dashboard enables notifications by default. Keep it running in a
separate terminal tab to hear or see session problems while working elsewhere:

```bash
codex-monitor dashboard
codex-monitor dashboard --test-notification
```

Each active conversation is tracked by owner endpoint and thread, so multiple
routes do not produce duplicate notices. Alerts identify the project and
conversation and report login failures (including authentication-related execution
errors), approval/input waits, execution errors, lost connections, unloaded
conversations, lost status visibility and recovery. Receiver loss is tracked
separately. Normal busy/idle changes and initial healthy or unverified states do
not notify. Existing actionable problems notify once when the dashboard starts.

Unchanged states never repeat. Disconnect, unload, lost-visibility and recovery
transitions require two consecutive successful snapshots. Authentication and
pending-decision notices fire on the next observed state change. Multiple changes
in one snapshot are combined into one bounded message. Recovery means that the
owner reports availability again, not that a failed task completed. Paused and
removed routes do not produce session alerts. A thread-filtered dashboard watches
only its visible conversations, plus receiver availability.

Notification transport can be selected explicitly:

```bash
codex-monitor dashboard --notifications auto
codex-monitor dashboard --notifications osc9
codex-monitor dashboard --notifications bel
codex-monitor dashboard --notifications desktop
codex-monitor dashboard --notifications off
```

`auto` uses the same portable mechanisms as
[Codex terminal notifications](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications):
OSC 9 where the terminal is recognized, and BEL otherwise. Monitor recognizes
`TERM_PROGRAM` values `iTerm.app`, `WezTerm` and `ghostty`, or `WT_SESSION`.
SSH often does not forward those hints, so use `osc9` explicitly if the local
terminal supports notification escape sequences. BEL uses the local terminal's
bell or activity indicator settings. OSC 9 travels through SSH to the terminal
on the user's computer; it does not require a desktop on the server. tmux OSC
passthrough is wrapped, but must also be permitted by the user's tmux settings.
No local client configuration is changed automatically. These options match
Codex's transport concepts, not every Codex focus/configuration behavior:
monitor sends on state transitions regardless of tab focus.

`desktop` is an optional host-local backend: Linux requires `notify-send`
(the libnotify tools package), a running desktop notification service and access
to its user session bus; macOS uses `/usr/bin/osascript`. Do not select it on a
headless SSH server to notify a different computer. Delivery runs outside the UI
loop with a bounded queue and command timeout. Failures show a dashboard notice;
a terminal bell is also sent. Popup visibility, sound and focus behavior remain
controlled by the terminal, desktop permissions and do-not-disturb settings.
A successfully emitted signal is not proof that the user saw a popup.

`--once`/JSON/status reads never emit notifications. Notification state is local
to each dashboard process, so reopening it can repeat currently actionable
problems. Notifications stop while that dashboard is closed, suspended inside
its opened Codex client, or disconnected from SSH; this is not a background
notification daemon. It never chooses answers, approves commands, refreshes
credentials, restarts an owner or starts a model turn. Pending-decision coverage
still depends on owner active flags; plain-text questions and subprocess password
prompts are not inferred.
