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

Direct WebSocket/Unix/daemon adapters can address the server that owns an already-loaded thread. Managed monitors now accept an explicit owner endpoint. An actual Unix remote-TUI measurement is recorded below; latency distributions and Desktop equivalence remain unverified. Desktop's current internal server cannot be assumed to be an arbitrary independently launched WebSocket server.

The present limitation is the verified shared-local path, not proof that every Codex integration must poll forever. Decreasing codex-monitor's HTTP or dispatch interval alone cannot remove the owner's external-queue scan interval.

## Managed Unix remote-TUI observation (2026-09-09)

One installed-wheel run used a test-owned Unix App Server with an ordinary
`codex --remote` TUI. The exact target thread was loaded on that server and the
monitor retained the supplied endpoint. It rendered and consumed the managed
file event and accepted a subsequent user turn.

| Observed interval | Seconds |
|---|---:|
| File change to local intake | 0.529 |
| Local intake to native acceptance | 0.002 |
| Native acceptance to observed consumption | 0.394 |
| Observed consumption to visible content | 0.108 |
| File change to visible content | 1.032 |

These timestamps come from sampled client observations and include observer
delay. They are not precise model-start measurements, p50/p95 statistics or a
latency guarantee. They do establish a functioning direct-owner CLI option;
shared-local behavior remains unchanged. The official
[App Server documentation](https://learn.chatgpt.com/docs/app-server)
classifies the transport as experimental and unsupported for production.

## Bounded latency canary

The opt-in `scripts/latency-canary.py` run samples the direct Unix-owner path
six times by default, cycling through idle, busy and post-receiver-restart
conditions. It keeps separate producer, checkpoint, acceptance, native-history
and PTY-rendering timestamps, correlates each sample by its exact delivery and
client IDs, and reports observed p50/p90/p95 values when a category has at
least two samples:

```bash
python3 scripts/latency-canary.py --run \
  --python /absolute/installed-venv/bin/python \
  --samples 6 --model gpt-5.6-luna --reasoning-effort xhigh \
  --report /tmp/codex-monitor-latency.json
```

The busy category requires the exact submitted user marker in an `inProgress`
turn returned by the official owner App Server. Idle checks also require no
active native turn; the composer can remain visible during streaming and is
not sufficient evidence of inactivity. The busy workload requests a 1,000-word
text response without tools. The restart category writes after the receiver
has restarted, so it measures a post-restart warm path. Quantiles are
descriptive for the bounded observations; with small per-category counts, p95
is unstable and no interval is a product guarantee.

## Six-sample direct-owner result (2026-09-09)

The final run passed all six samples in 105.276 seconds with the predicate
runtime and Luna at xhigh. Each event was accepted, correlated with one exact
native client ID, inspected as consumed, and rendered in the ordinary TUI.
The test-owned conversation was archived and its processes were stopped.

| Scenario | Samples | Min–max observed seconds | Observed p50 | Observed p95 |
|---|---:|---:|---:|---:|
| Idle | 2 | 0.793–1.150 | 0.971 | 1.132 |
| Generating a 1,000-word response | 2 | 30.005–33.051 | 31.528 | 32.899 |
| After receiver restart | 2 | 0.585–0.633 | 0.609 | 0.631 |

These are file-change-to-rendering observations. The busy samples entered local
storage within 0.590 seconds, then waited roughly 29.626–32.455 seconds between
observed native acceptance and consumption. Direct delivery improves the owner
notification path but does not promise immediate processing during an active
turn. The monitor preserves the user's turn rather than forcing interruption.

Two observations per scenario are too few to estimate a dependable tail. The
interpolated p95 values describe this run only. This is neither a matched Claude
benchmark nor new Desktop evidence. Earlier failed attempts are preserved:
screen-based idle/busy detection missed streaming turns when the composer
remained visible or the submitted marker scrolled away. The final run uses
official native turn state and exact input correlation instead.

## Improvement acceptance criteria

1. Record producer timestamp, HTTP receipt, native queue acceptance, owner consumption and visible response separately.
2. Measure p50/p95 and worst observed delays under idle, busy and restart scenarios.
3. Evaluate officially supported same-owner notification or hosted event ingress for each client; retain queue durability and user interruption semantics.
4. Require actual ordinary TUI and Desktop validation. No private Desktop IPC, forced turn start or periodic model wakeups as a substitute.

Sources and version limitations: [compatibility](COMPATIBILITY.md), [Claude comparison](CLAUDE-COMPARISON.md), and [TODO](TODO.md).
