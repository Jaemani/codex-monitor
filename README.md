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
- Runtime installation, upgrade and uninstall on macOS and Linux; 75 regression tests; hosted CI on Python 3.11/3.14.

Desktop draft/approval contention, Windows/WSL, Desktop SSH projects, OS sleep/reboot and fresh-client skill discovery still need validation. Protocol tests do not substitute for these cases. The project does not claim overall parity with Claude Channels; see the [comparison](docs/PRODUCT-COMPARISON.md).

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
See [conversation-scoped monitors](docs/CONVERSATION-MONITORS.md) for lifecycle, isolation and limits.

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

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python scripts/check-publication.py
.venv/bin/python -m unittest discover -s tests -v
```

Actual model and UI canaries are opt-in and require an explicitly selected test conversation. See [testing](docs/TESTING.md). Keep code, TODO and measured verification results together when updating the project. Raw conversation transcripts and local runtime data are excluded from Git.

Read [contribution guidelines](CONTRIBUTING.md) before preparing a public update.
