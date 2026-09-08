# Event latency: Claude Channels and the current Codex path

The distinction is how an already-running conversation learns that an event exists. It is not the receiver's HTTP speed or periodic model calls.

## Claude Channels

```text
external producer → channel MCP server
                  → notification on the session's open MCP transport
                  → Claude session event handler → process when eligible
```

A running, opted-in session maintains the connection. The server can push a notification without waiting for a timer. This does not imply immediate model output: an active turn, permission state, batching and model latency can delay processing. See the official [Channels reference](https://code.claude.com/docs/en/channels-reference).

## codex-monitor shared-local (Codex 0.153.4)

```text
external producer → authenticated receiver → durable monitor inbox
                  → independent stdio App Server writer → official shared queue
                  → owning CLI/Desktop periodically observes external changes
                  → owning conversation processes queued input when eligible
```

The writer and conversation owner are different processes. Queue persistence is available through the official API without loading or resuming the conversation in the writer. In the inspected Codex version, `watch_external_messages` checks external queue changes at roughly 10-second intervals. This is a native data-plane check, not a model prompt. An accepted receipt can therefore precede the owner's observation by a polling interval; scheduling, retries and active work can add more delay. Ten seconds is not an end-to-end upper bound.

Local Desktop does not require SSH. SSH is a separate transport option for an existing remote development host.

## Why not replace it with MCP push immediately?

The installed Codex returned `MCP event subscriptions are only supported for hosted apps` for `mcpServer/event/stream/start`. Registering a normal local MCP server therefore did not provide the same event ingress as Claude Channels. No supported local plugin mechanism for hidden channel input or a persistent monitor badge was confirmed.

Direct WebSocket/Unix/daemon adapters can address the server that owns an already-loaded thread. They may offer a different notification path, but their lower latency across ordinary CLI and Desktop is not established. The real Unix remote-TUI baseline verifies functionality, not a latency advantage. Desktop's current internal server cannot be assumed to be an arbitrary independently launched WebSocket server.

The present limitation is the verified shared-local path, not proof that every Codex integration must poll forever. Decreasing codex-monitor's HTTP or dispatch interval alone cannot remove the owner's external-queue scan interval.

## Improvement acceptance criteria

1. Record producer timestamp, HTTP receipt, native queue acceptance, owner consumption and visible response separately.
2. Measure p50/p95 and worst observed delays under idle, busy and restart scenarios.
3. Evaluate officially supported same-owner notification or hosted event ingress for each client; retain queue durability and user interruption semantics.
4. Require actual ordinary TUI and Desktop validation. No private Desktop IPC, forced turn start or periodic model wakeups as a substitute.

Sources and version limitations: [compatibility](COMPATIBILITY.md), [Claude comparison](CLAUDE-COMPARISON.md), and [TODO](TODO.md).
