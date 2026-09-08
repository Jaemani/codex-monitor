# Live connection dashboard

The dashboard is a separate terminal view of one local codex-monitor state directory. It reads
configured conversations, managed collector observations, delivery receipts and explicit request
reports. It does not call the Codex App Server, create model turns or change monitoring configuration.

```bash
MONITOR="$HOME/.local/share/codex-monitor/bin/codex-monitor"
"$MONITOR" dashboard
"$MONITOR" dashboard --thread "$THREAD_ID"
"$MONITOR" dashboard --once
"$MONITOR" dashboard --once --json
```

Use `--state /absolute/state` before `dashboard` for a different local receiver. The default refresh
interval is two seconds; `--interval SECONDS` changes it. Live mode needs an interactive terminal.
Use snapshot mode in scripts and redirected output.

## Controls

- `q` or Ctrl-C: leave the dashboard. The receiver and monitors continue running.
- `j` / `k` or down / up: scroll through the inventory.
- Resize the terminal to fit your workspace; the next render adapts to its dimensions.

The dashboard preserves the terminal's input settings on normal exit. Its read-only view cannot
pause bindings, replay events, send replies or restart the receiver. Use explicit CLI operations
for those actions after inspecting the relevant conversation and receipt.

## Reading the screen

| Field | Meaning |
|---|---|
| Receiver process | Whether the receiver's local process lock is held |
| Authenticated status | Result of a bounded loopback `/v1/status` check; HTTP errors and worker errors are distinct from process liveness |
| Conversation and bindings | Stored routing configuration in this state directory; enabled or paused does not establish client presence |
| Collector observations | Persisted local sampler status, observation age and errors; old observations must not be treated as current activity |
| Events | Stored states such as pending, submitting, uncertain, accepted or dead; counts are not counts of completed tasks |
| Latest receipt | Identity and age of the latest stored delivery; use `inspect DELIVERY_ID` for explicit native queue/history evidence |
| Requests | Explicitly reported request states and latest report; not automatically inferred agent execution state |
| External producer health | Unknown until a supported producer observation exists; successful old events are not a live connection check |

The screen shows registered connections across conversations, not every native Codex agent. It does
not infer model selection, token use, current generation or successful work from event traffic. A
thread filter selects the inventory to display; it does not retarget any delivery.

## Failures and scope

Missing, unreadable or incompatible databases appear as inventory errors. Live mode can retry after
the state becomes available. A stopped receiver does not erase its saved connections. Native target
checks are intentionally outside the refresh loop, keeping dashboard reads independent of model work.

SQLite is opened in read-only mode without invoking runtime constructors or migrations. Local reads
and status probes are bounded; the HTTP check stays on configured loopback, does not use environment
proxies, and does not follow redirects. Token contents and event bodies are not displayed.

This dashboard is not a cross-host fleet manager or a native Codex badge. Start a view for each state
you operate. External producer telemetry and a richer native agent-activity integration are separate
future features. OS-timed probes can already feed failure events using the [health-hook example](HEALTH-HOOK.md).

Validation is recorded in [TESTING.md](TESTING.md). A dashboard PTY test verifies this terminal view,
not a Codex CLI conversation or Desktop event-delivery flow.
