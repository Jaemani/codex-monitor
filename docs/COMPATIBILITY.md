# Client connectivity and validation scope

Current development priority (2026-09-09): CLI conversations on one explicitly shared App Server.
`resident --endpoint ENDPOINT --thread THREAD_ID` maintains owner subscriptions and reconnects to
that owner. This is distinct from the default shared-local queue writer. The foreground owner and
resident require supervision for unattended process restarts; see [owner lifecycle](OWNER-LIFECYCLE.md).
Default local Desktop still lacks a verified supported owner endpoint for this feature. Previous
successful Desktop deliveries cover loaded targets, not automatic wakeup of arbitrary unloaded tasks.

The 2026-09-09 JSON predicate extension passed installed macOS/Linux process
checks and actual ordinary default shared-local and Unix remote TUI
matched/recovery/follow-up flows.
Live receivers advertise capabilities; new predicate creation and request
mutations refuse an older active receiver. This does not make older binaries
safe against newer state, and adds no Desktop pixel or Windows evidence.

Commands using `.venv/bin` assume the development environment described in the [README](../README.md#development). For a runtime installation, use the executable path printed by the installer.

Status as of **2026-09-08**. The goal is to continue the same conversation from both CLI and Desktop. The default path is Codex's official `shared-local` queue. Earlier guidance incorrectly required a connection to the same Desktop server. Local Desktop has a native consumer for external queue changes. Its responses have been verified, and the user observed event visibility and Desktop exit/reopen behavior. Automated pixel checks and Desktop draft and permission-prompt tests remain incomplete.

| Path | Implementation | Verification |
|---|---|---|
| Local CLI/Desktop → `shared-local` | Independent stdio writer and official shared queue | Two-server model PASS; real CLI TUI PASS; current Desktop processed and answered one ordinary event and one installed-service event |
| CLI → `ws://127.0.0.1:8765` shared server | Host/connect and queue adapter | Real Codex 0.153.4 model canary PASS |
| Real CLI TUI `--remote` → Unix server | Direct queue adapter on the same server | Real TUI user input, draft, idle, exit, and resume PASS |
| Existing `local` daemon for standard CLI | `codex app-server proxy` | Supported in code; requires a running daemon |
| Desktop SSH project → `ssh://HOST` | SSH proxy adapter | Awaiting a provided host and real connection test |
| Custom `unix:///...` | WebSocket over Unix with compression negotiation disabled | Bundled native server initialize and loaded-list PASS; stable connection and clean shutdown verified |
| Linux | Native CLI TUI and shared receiver | Debian 12 aarch64 clean-wheel session UX: 61 tests plus HTTP/lifecycle and real Codex 0.153.4 TUI baseline PASS |
| Windows/WSL | Shared Python core and TCP path | Awaiting real-device validation |

Direct WebSocket and daemon adapters require the same server to have the thread loaded. **Shared-local enqueue does not require loading the thread in the writer; actual consumption still requires a loaded owner in the target client.** It writes input through the official queue API for the same OS user and Codex store. The server that owns the loaded conversation consumes it. An open Desktop app, a saved task in the sidebar, or an enabled binding alone does not establish that ownership. The writer's loaded-thread list may be empty. It does not call `thread/start`, `thread/resume`, or `turn/start`. If the client closes, the input remains queued and may be processed when the conversation is opened again.

Managed local file collectors use the same delivery path. The new installed wheel passed 75 tests and 16 receiver/collector process checks on macOS and Linux; an ordinary macOS TUI consumed its managed event and answered a user follow-up. The managed Desktop event subsequently arrived in the same conversation after the preceding assistant turn ended. These results do not add new Desktop pixel or Linux managed-TUI evidence.

## Compatibility and unattended operation

The verified baseline is Codex 0.153.4, not a promise that every version before or after it supports
the same experimental API. A Windows deployment reported that 0.147.0 lacks `thread/queue/list`:
its OS health probe continued working while delivery became `dead`. An isolated official 0.147.0 macOS binary reproduced the missing method with JSON-RPC `-32600`
and `unknown variant thread/queue/list`; no model call or host CLI replacement was used. The Windows
host itself has not been independently retested. Check the exact target with `doctor` before relying on
an outage hook; receiver liveness is independent of Codex API compatibility.

A separate Desktop incident had native `queued` input for more than six minutes while the target
reported `notLoaded`, with no new processing turn. The writer and Desktop returned identical latest
turn IDs. This is consistent with a missing loaded consumer, not proof of every current process's
storage configuration. The original event was not replayed or force-started; successful consumption
and a substantive reply remain unresolved.

In inspected 0.153.4 source, `watch_external_messages` filters by `ThreadManager.list_thread_ids()`.
`wake_if_loaded` does nothing when the manager cannot find the thread. The upstream
`cold_thread_resume_dispatches_a_persisted_queued_submission` test explicitly expects `NotLoaded`
and a retained queue item before a resume. These are source findings, not a newly executed Desktop test:

- [Queue service](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/queue/src/service.rs)
- [Cold-thread test](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/tests/suite/v2/thread_queue.rs)
- [Official thread lifecycle](https://learn.chatgpt.com/docs/app-server#api-overview)

**Arbitrary unloaded Desktop tasks cannot currently be advertised as unattended event responders.**
An always-available receiver provides durable intake, not a permanent model/session owner. A CLI
TUI or explicit owner server must keep the intended conversation loaded; its lifecycle still needs
verification. Automatic loading/ownership is a separate integration requirement, not a dispatch retry.
See [troubleshooting](TROUBLESHOOTING.md) for safe diagnosis.

## Evidence for the shared local queue

- The `queue_cmd.rs` and `session_queue_commands.rs` sources for installed version `rust-v0.153.4`: `codex queue` uses `thread/queue/add` even through a separate embedded server.
- `thread_queue_processor.rs`: add and list work with stored conversations without resuming them.
- `ext/queue/src/service.rs`: `watch_external_messages` checks the shared queue about every 10 seconds and wakes only conversations already owned by that server. The check itself does not invoke the model.
- Two-server canary PASS: `docs/evidence/shared-local-canary-2026-09-08.json`.
- The current Desktop conversation's native history contains one official queue event, one response, and one occurrence of the client ID: `docs/evidence/desktop-live-2026-09-08.json`.
- Desktop also processed and answered one event from the installed macOS service: `docs/evidence/desktop-launchd-2026-09-08.json`.

Public implementation: https://github.com/openai/codex/tree/rust-v0.153.4/codex-rs/ext/queue

The direct-socket notes below are historical. They do not show that `shared-local` is unavailable, and the default path does not require SSH or another shared socket.

## Historical Desktop direct-socket investigation

### Owner attachment follow-up (2026-09-09)

A subsequent recheck still found CLI and Desktop binaries at 0.153.4, no default control socket,
and a failed `local` doctor connection. The current Desktop conversation passed the shared-local
queue probe, with consumer readiness explicitly unknown. Official App Server and remote-connection
documentation still did not establish an external attachment API for the existing local Desktop owner.
Computer Use again refused access to the Desktop app; no alternative UI automation was used.
The optional `doctor --require-consumer` check now prevents an unknown consumer from passing an
unattended setup gate. It is a diagnostic improvement, not a cold-task wakeup implementation.

The installed Desktop bundle and CLI both report Codex 0.153.4. The Desktop child still has no
`--listen` argument, and the official `local` doctor probe returns `ready:false` with no fallback.
This is an owner-access boundary, not a demonstrated CLI/Desktop version mismatch.

Current official documentation provides no verified replacement for that missing connection:

| Candidate | Documented behavior | Limit for unattended unloaded tasks |
|---|---|---|
| [Async command hooks](https://learn.chatgpt.com/docs/hooks) | Completion during idle waits for the next user turn; finishing a background hook does not start a new turn | Cannot supply a cold-task wakeup |
| [MCP integration](https://learn.chatgpt.com/docs/extend/mcp) | Tools, context and transports configured for clients | No documented arbitrary external-event contract that loads a Desktop task |
| [Desktop developer settings](https://learn.chatgpt.com/codex/developer-settings) | Shared MCP configuration | No documented arbitrary local App Server attachment setting found |
| [Remote connections](https://learn.chatgpt.com/docs/remote-connections) | Human remote continuation and Desktop-managed SSH projects | Does not document third-party wakeup of arbitrary local Desktop tasks |
| [Custom App Server client](https://learn.chatgpt.com/docs/app-server) | Explicit owner connection and `thread/resume` subscription | Enables the CLI resident design; does not expose the existing Desktop owner |

These are documentation findings, not new Desktop delivery tests or proof that future integration
is impossible. The App Server command and WebSocket transport are explicitly experimental and
unsupported for production workloads upstream. The CLI implementation uses those public APIs;
its real-client tests do not override that upstream support policy. No private IPC or application
patching was used in this follow-up.

### Local retest before discovering `shared-local` (2026-09-08)

This investigation covered only direct Desktop socket or daemon access. Its failures do not block `shared-local`, which does not require the Desktop server endpoint. SSH is an optional adapter for remote projects.

- Rerunning `doctor --surface desktop --endpoint local` exited 2 with `ready:false` and `fallback_used:false`.
- `codex app-server daemon version` failed because the default control socket was absent.
- The running Desktop child Codex process had neither an `app-server` subcommand nor a `--listen` argument. Official App Server documentation says the default transport is stdio. These observations are not evidence of a shared WebSocket endpoint.
- The public Desktop Settings, Configuration Reference, and Environment Variables documentation did not reveal a setting that redirects the current local Desktop app to an arbitrary shared App Server endpoint. Absence from those documents does not prove technical impossibility in every version.

The earlier conclusion expanded “no observed direct transport” into “no local Desktop connection path.” Public source analysis and real testing later established the official shared queue path above.

Additional first-party references:

- https://learn.chatgpt.com/docs/reference/settings
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/config-file/environment-variables

Environment and evidence limits:

- Installed app: `com.openai.codex`, displayed as ChatGPT, version `26.901.51231`, build `8109`.
- The default CLI daemon socket `~/.codex/app-server-control/app-server-control.sock` was absent.
- `codex-monitor doctor --surface desktop --endpoint local` returned `ready:false` with no fallback.
- Computer Use declined control of the Desktop and Terminal apps. No alternative UI automation or private IPC was used. Automated Desktop pixel validation was therefore unavailable. The user later observed app exit/reopen and event visibility. A real CLI TUI was tested separately in a test-owned PTY.
- A personal MCP plugin event-stream workaround also failed. The installed CLI returned `MCP event subscriptions are only supported for hosted apps` with code `-32600` for `mcpServer/event/stream/start`. Registering a normal local MCP server does not enable that feature.

A `protocol-ready` result from `doctor` is not UI validation. It means the monitor can address a specified thread and inspect the queue contract; the result also reports `client_ui_verified:false`.

## Optional official Desktop SSH path

Official documentation supports adding an existing SSH host and remote project under **Desktop Settings → Connections**. Codex must be available from the remote login shell. If that Desktop connection uses a shared daemon, the monitor can connect to the same daemon.

```bash
# Run against an existing, trusted SSH host.
ssh devbox codex app-server daemon version
.venv/bin/codex-monitor doctor --surface desktop   --endpoint ssh://devbox --thread "$DESKTOP_THREAD_ID"
```

The `ssh://` adapter accepts only an existing SSH alias and uses BatchMode and StrictHostKeyChecking. It does not enable SSH, deploy keys, trust a host key, or grant remote-control access. If daemon bootstrap is needed, the host operator may review `codex app-server daemon bootstrap`. Confirm that the target thread is loaded; daemon availability alone does not prove that Desktop uses it.

## Real-client acceptance test

Run this only with a disposable conversation prepared for Desktop pixel/manual visibility, restart, draft, and permission-prompt testing.

```bash
.venv/bin/python scripts/interactive-canary.py --run   --surface desktop --endpoint "$ENDPOINT" --thread "$TEST_THREAD_ID"   --report /tmp/codex-monitor-desktop.json
```

Enter the script's READY text in that client. After the event response appears there, enter its AFTER text. The report checks user input before and after the event response in the same conversation. Use `--surface cli` for CLI. The automated real-TUI PTY acceptance test has already passed. Never inject a test event into an unrelated working conversation.

Desktop native consumption and response are verified. The screen and state-concurrency cases above are not yet recorded as PASS.

First-party references:

- https://learn.chatgpt.com/docs/app-server#connect-the-cli-terminal-ui
- https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host
- Queue and thread schemas from installed CLI command `codex app-server generate-json-schema --experimental`

The 2026-09-09 process-sampler/debounce wheel passed 100 tests and 17 installed isolation/condition checks on macOS and Debian 12 arm64/Python 3.11. These results establish sampler recovery and condition behavior with a fake App Server peer; they do not extend Desktop screen, Windows/WSL or Linux TUI coverage.

The subsequent request/explicit-endpoint wheel passed 124 macOS/Linux regression
tests and actual ordinary macOS request-lifecycle TUI checks. Managed monitor
delivery through an owned Unix App Server and `codex --remote` also passed.
Other explicit endpoint forms are implemented and validated syntactically;
this does not add SSH, Windows or Desktop screen evidence. Upstream documents
App Server WebSocket transport as experimental and unsupported for production.
