# Client connectivity and validation scope

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

Direct WebSocket and daemon adapters require the same server to have the thread loaded. **`shared-local` does not.** It writes input through the official queue API for the same OS user and Codex store, and the server that owns the conversation consumes it. The writer's loaded-thread list may be empty. It does not call `thread/start`, `thread/resume`, or `turn/start`. If the client closes, the input remains queued and may be processed when the conversation is opened again.

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
