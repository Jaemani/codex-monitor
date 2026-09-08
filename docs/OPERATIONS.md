# Operations and recovery

## State and permissions

The `--state` directory contains `config.json`, admin and source token files,
`monitor.sqlite3` and its WAL, `serve.lock`, and watcher checkpoints. The
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

Set positive integer limits in `config.json`, then restart `serve`:

```json
{"max_attempts":5,"max_age":3600,"max_pending":1000,"rate_limit":120,"trace_limit":16}
```

`rate_limit` is the number of new events per binding in the last 60 seconds;
duplicate retries do not consume quota. `max_pending` counts
`pending`/`submitting`/`uncertain` events per binding. `trace_limit` caps events
with one trace across all bindings. The hop limit of 8 is separate.

Records are not deleted automatically. Deleting old records also removes
deduplication evidence, so define a retention policy and monitor state volume.
Stop `serve` before making a consistent backup of the database and WAL.

## Long-running service

The default `shared-local` path needs only `serve`. Keep `host` separately
running only when a direct WebSocket connection is required. Run the same
absolute command under the OS service manager; this is a systemd example:

```ini
[Unit]
Description=Codex Monitor event receiver
[Service]
Type=simple
ExecStart=/absolute/installed-venv/bin/codex-monitor --state /absolute/state serve
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
```

On macOS, use the `service` command from a wheel-installed console entry point.
Editable/source execution cannot be registered as a persistent service:

```bash
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service install
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service status
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service restart
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service stop
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service start
/absolute/installed-venv/bin/codex-monitor --state /absolute/state service uninstall
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

The macOS install, forced-termination recovery, restart, reinstall, removal,
and queue-delivery paths have been tested. Test jobs are removed after testing;
the project does not leave a permanent receiver running for the user.

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
