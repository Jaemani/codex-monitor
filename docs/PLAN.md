# codex-monitor implementation plan

Goal: keep an interactive Codex conversation usable by its human while external events wake that exact conversation only when needed. No scheduled model polling and no exec/resume worker substitution.

The user authorized comprehensive test design. Public test seams are the authenticated event HTTP/CLI API, durable delivery/status/recovery API, Codex App Server RPC boundary, and an interactive client sharing the same thread. Test fixtures stand in for external senders and client transports; private database layout is not a test contract.

## Vertical slices

1. Receive authenticated webhook, persist once, deliver to a selected live thread; zero delivery calls while idle.
2. Queue behind a user turn; retain user input and selected thread; reconcile queue and transcript after response loss.
3. Deduplicate and order ingress, bounded retries/dead letters, stop/restart, rate and loop limits.
4. CLI adapters for agent messages and change-only monitoring; generic JSON webhook contract for extension.
5. CLI interactive canary, Desktop endpoint diagnosis and compatibility matrix, operator setup and recovery guide.

## Test matrix

- Empty/idle, one event, random webhook burst, duplicate and conflicting IDs, invalid schema/auth/size.
- User active, user queue ahead, approval waiting, event/user arrival races, multiple conversations.
- Agent A/B exchange with trace/hop limits; ACK and status observations do not auto-echo.
- Monitoring initial baseline, change, unchanged, recovery, command failure (explicit argv, no remote command execution).
- Failure before submission, queue accepted but response lost, server disconnect/reconnect, process restart.
- Crash after acceptance; delivery reconciliation by client message ID, ambiguous state pauses instead of blind replay.
- Queue deletion by user, binding removed/disabled, unavailable Desktop endpoint, unsupported protocol.
- Seeded random order/retry stress and repeatable standalone opt-in real-client canaries.

## Platform boundary

CLI 0.153.4 exposes --remote, codex queue and experimental App Server queue RPCs. Current Desktop app is com.openai.codex (26.901.51231). Its shared app-server socket is absent, and the CUA tool refuses control of that app. Do not bypass the UI restriction or infer Desktop compatibility from a CLI success. Desktop support requires a reachable supported App Server endpoint for its actual conversation; record the diagnostic result and remaining manual verification explicitly.

Official source: https://learn.chatgpt.com/docs/app-server. RPC schemas generated from installed codex-cli 0.153.4, including experimental queue methods.

## Implementation status (2026-09-08)

- Implemented standalone installable Python package with CLI, authenticated HTTP ingress, durable dedup/FIFO,
  bounded retries and operator resolution, queue/history reconciliation, source/trace budgets, change watcher.
- 22 deterministic tests and 20-seed stress (2,000 unique events / 6,000 submissions) passed.
- Real App Server canary passed on loopback WebSocket: same-thread user/event coexistence, idle silence,
  duplicate suppression, history reconciliation and no workspace changes.
- Local Desktop is NOT complete: no supported shared endpoint exposed; UI automation refused; local MCP event
  stream request explicitly rejected with "only supported for hosted apps".
- A Desktop SSH host alias was requested asynchronously; no target has been supplied yet.
- Remaining completion gates: actual CLI/Desktop UI acceptance via interactive-canary, Desktop shared endpoint
  provision/selection, and OS-specific deployment verification where claimed. Do not mark the combined goal complete.
