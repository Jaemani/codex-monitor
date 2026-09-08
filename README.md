# codex-monitor

Deliver external events into **the same Codex conversation** while the user keeps working in it. Idle monitoring does not send check-in prompts or create model turns.

[Project TODO](docs/TODO.md) · [Status](docs/STATUS.md) · [Compatibility](docs/COMPATIBILITY.md) · [Verification summary](docs/evidence/README.md)

## How it works

```text
webhook / agent / change detector
              ↓ authenticated event
    codex-monitor durable inbox
              ↓ official queue API
       existing conversation queue
              ↓ native consumer
       ordinary CLI TUI or Desktop
```

The default `shared-local` adapter uses an independent App Server writer and the client's existing conversation store. It does not start or resume threads, start turns, interrupt the user, or approve tools. Local Desktop does not require SSH or a separately configured shared socket.

In the validated Codex version, the native consumer checks external queue changes at roughly 10-second intervals. Events can remain queued while the client is closed and be consumed when the same conversation is reopened. Queue acceptance is not proof of consumption or successful model work. See [latency and push delivery](docs/LATENCY.md).

## Current scope

Validated against Codex CLI **0.153.4**, using experimental App Server queue APIs:

- Actual ordinary TUI on macOS and Debian arm64, including user/event interleaving, unsent drafts, idle silence and restart/resume.
- Unix remote TUI, a one-hour TUI soak and a one-hour receiver outage test.
- Desktop same-conversation delivery, explicit reply round trip, and user-observed app restart and event visibility.
- Runtime installation, upgrade and uninstall on macOS and Linux; 141 local regression tests; hosted CI on Python 3.11/3.14 previously passed 140 tests.

Desktop draft/approval contention, Windows/WSL, Desktop SSH projects, OS sleep/reboot and fresh Desktop skill discovery still need validation. Fresh ordinary TUI skill autocomplete and an earlier installed basic file-monitor create/status/pause/resume/remove workflow passed; the latest skill revision still needs fresh-client behavioral validation. Protocol tests do not substitute for these cases. The project does not claim overall parity with Claude Channels; see the [comparison](docs/PRODUCT-COMPARISON.md).

## Install

Requires Python 3.11+ and Codex CLI. From a trusted checkout:

```bash
git clone https://github.com/Jaemani/codex-monitor.git
cd codex-monitor
python3 scripts/install.py --with-skill install
```

The installer prints the runtime path, normally `~/.local/share/codex-monitor/bin/codex-monitor`. It does not change your shell configuration. The only runtime dependency is `websockets`; installation may fetch dependencies from your configured Python package index.

Start a new Codex session for skill discovery and request, for example:

```text
$codex-monitor Receive build-failure events in this conversation.
```

The skill manages the installed runtime. Installation alone does not start a receiver or an event producer. See [installation, upgrades and removal](docs/INSTALLATION.md).

## Usage examples

These are example requests for the installed skill, not claims that every external integration is
bundled. Use a real file path or an already configured producer. Each monitor belongs to an explicitly
chosen conversation; several monitors can feed one conversation, and different conversations can
have independent monitors.

### Keep working while a build finishes

```text
$codex-monitor Watch /absolute/project/build-status.json in this conversation.
Notify me when its contents change, and keep unchanged observations quiet.
```

The managed collector sends change metadata, not file contents. Ask the conversation to read the
file when needed. Its first observation establishes a silent baseline.

### React only when a condition changes

```text
$codex-monitor Watch /absolute/project/build-status.json. Notify this conversation
when /build/status becomes "failed" for five seconds, and when it recovers.
```

JSON predicates and debounce run outside the model. The initial valid observation stays silent even
if already matched; request a separate initial status check if needed. Invalid samples are errors,
not successful matches. See [condition policies](docs/CONVERSATION-MONITORS.md).

### Give a PM conversation updates from several workers

```text
$codex-monitor Receive events from my configured worker sources in this PM conversation.
Act on blocked, failed and completed work. Preserve the worker identity and request ID.
Keep ordinary progress chatter out of the event stream.
```

Workers need an adapter that emits authenticated events with stable IDs and filters routine updates.
Attaching a source does not launch a worker or discover every Codex agent. Use the
[request lifecycle](docs/REQUEST-LIFECYCLE.md) for explicit progress and terminal states; message
delivery alone does not complete the request.

### Use an optional relay conversation

```text
$codex-monitor Use this conversation as the receiver for my configured messaging source.
Forward relevant requests, their original content and metadata to the PM conversation I specify.
Let the PM provide the substantive response; send it back only within my authorized reply workflow.
```

Choose the relay's model in Codex if you want routing and PM reasoning to use different models.
A relay is optional: the PM can receive events directly. Discord requires a separate Gateway
producer and reply adapter; these are not bundled Discord features. Producer authentication, source
scoping and explicit replies still apply. See [adapter contracts](docs/ADAPTERS.md).

### Check or pause one conversation's monitoring

```text
$codex-monitor Show the monitors in this conversation, receiver state, collector errors
and last delivery evidence. Distinguish unknown source health from a confirmed failure.
```

```text
$codex-monitor Pause the build monitor in this conversation. Leave other monitors running.
```

Status commands do not create model turns; asking an assistant to interpret them uses an ordinary
conversation turn. Pause affects future delivery and cannot retract input already accepted by Codex.

## Manage a file monitor for this conversation

Use an explicit conversation ID, or omit `--thread` only when the Codex host supplies `CODEX_THREAD_ID`:

```bash
MONITOR="$HOME/.local/share/codex-monitor/bin/codex-monitor"
"$MONITOR" init
"$MONITOR" doctor --thread "$THREAD_ID"
"$MONITOR" monitor create build --thread "$THREAD_ID" --file /absolute/project/build-status.json
"$MONITOR" serve
```

From another terminal, use `monitor list`, `monitor status build`, `monitor pause build`,
`monitor resume build` or `monitor remove build`, with the same `--thread` and state.
The receiver owns these collectors. Creating a definition alone does not start the receiver.
Use `--debounce 5` to require a changed sample to remain stable for five seconds before delivery. See [conversation-scoped monitors](docs/CONVERSATION-MONITORS.md) for lifecycle and [structural limits](docs/RELIABILITY-LIMITS.md) for isolation, recovery and native-client constraints.

For external work requests, the [request lifecycle](docs/REQUEST-LIFECYCLE.md)
links the original receipt to explicit progress, completion, failure and expiry.
The receiver sends meaningful state changes to the same conversation; receiving
an event does not automatically mark the underlying work complete.

## Attach an existing conversation

Use the exact existing conversation ID. The monitor and client must use the same OS user and Codex store (`CODEX_HOME` / `sqlite_home`).

```bash
MONITOR="$HOME/.local/share/codex-monitor/bin/codex-monitor"
"$MONITOR" init
"$MONITOR" source webhook
"$MONITOR" doctor --thread "$THREAD_ID"
"$MONITOR" attach work --thread "$THREAD_ID" --source webhook
"$MONITOR" serve
```

Set `THREAD_ID` to the intended conversation ID before running these commands. `serve` runs in the foreground; use the documented [service workflow](docs/OPERATIONS.md) for persistent operation. The receiver supervises managed file collectors; external producers and legacy `watch-file` processes need their own lifecycle.

State defaults to `~/.local/state/codex-monitor`. Use `--state /absolute/path` or `CODEX_MONITOR_HOME` for another location. Source tokens are stored in private files; the CLI prints their paths, not token values. Existing state and credentials are preserved.

## Send and inspect events

With the receiver running, use another terminal:

```bash
MONITOR="$HOME/.local/share/codex-monitor/bin/codex-monitor"
"$MONITOR" send --to work --source webhook \
  --type build.failed --id build-2026-001 --data '{"job":17,"result":"failed"}'
"$MONITOR" sessions work
"$MONITOR" inspect "$DELIVERY_ID"
"$MONITOR" pause work
```

External producers can POST to `http://127.0.0.1:8766/v1/events/work` using their source token in the `Authorization: Bearer <source token>` header and `Content-Type: application/json`:

```json
{"id":"build-2026-001","source":"webhook","type":"build.failed","data":{"job":17,"result":"failed"}}
```

Identical binding/source/event IDs and content return the existing receipt. Reusing an ID with different content returns HTTP 409. Events are untrusted data and cannot grant permissions. Replies are explicit and scoped to the original source; assistant output is not automatically forwarded.

See [session management and replies](docs/SESSION-WORKFLOW.md), [adapters](docs/ADAPTERS.md), and [cancellation behavior](docs/CANCELLATION-QUEUE.md).

## Is monitoring working?

Ask `$codex-monitor check monitoring status for this conversation`, or run
`"$MONITOR" sessions work` for an external binding. For managed files use
`"$MONITOR" monitor status NAME --thread "$THREAD_ID"`.

| Observation | What it establishes |
|---|---|
| Binding enabled | Delivery is configured for that conversation |
| Receiver running | Receiver process exists; check authenticated HTTP readiness separately |
| Managed collector observation | Last observed sampling state and errors |
| External producer unknown | Check the producer's own connection/health; old deliveries do not establish current health |
| Receipt accepted / native consumed | Codex accepted input / input reached conversation history; neither proves completed work |
| Intended action and destination reply verified | That particular end-to-end workflow succeeded |

Event monitoring uses a receiver and a producer outside the model instead of scheduled model prompts.
The chosen conversation processes arriving events, with native queueing while busy. A separate relay
conversation is optional, useful when deliberately separating routing from PM work. Installation alone
does not configure a source or select the conversation. A native monitoring badge is not provided.
Ordinary replies can stay concise; `event DELIVERY_ID` retains the full submitted envelope for explicit
forwarding and `inspect DELIVERY_ID` provides transport diagnostics.

### Multiple conversations and a live status screen

Today, `"$MONITOR" sessions` lists all bindings in the selected state directory without contacting
the model. Use `"$MONITOR" sessions --json` for structured snapshots and conversation-scoped
`monitor list/status` for managed collectors. This is a configured-connection inventory, not automatic
discovery of every running agent, a cross-host fleet view, or proof that a client is currently open.

A separate read-only terminal dashboard is the recommended next interface. It would refresh local
status without adding messages to conversations or consuming model turns. The proposed columns are
conversation/binding, delivery enabled or paused, receiver readiness, observed producer health and
observation age, pending or failed deliveries, and latest receipt. Worker activity should appear only
when explicitly reported by a supported integration. Unknown and stale observations must remain visible.

**This dashboard is planned, not implemented.** A native CLI/Desktop monitoring badge is not provided
by this project. See the [visibility backlog](docs/TODO.md). A live display of stored observations
cannot establish external producer health until the producer reports it.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python scripts/check-publication.py
.venv/bin/python -m unittest discover -s tests -v
```

Actual model and UI canaries are opt-in and require an explicitly selected test conversation. See [testing](docs/TESTING.md). Keep code, TODO and measured verification results together when updating the project. Raw conversation transcripts and local runtime data are excluded from Git.

Read [contribution guidelines](CONTRIBUTING.md) before preparing a public update.
