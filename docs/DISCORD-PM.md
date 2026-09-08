# Discord project PM: operating levels

This is an application example; the core roadmap follows [monitoring technology levels](MONITOR-LEVELS.md). This is a proposed implementation sequence for a project PM or relay agent that uses Codex and Discord. Discord integration is not implemented yet. The existing monitor provides durable event delivery into a selected Codex conversation; it does not infer worker progress or keep a closed Codex client running.

## Levels and examples

| Level | Example | Readiness gate |
|---|---|---|
| 1. Event delivery foundation | A worker publishes a failure; the selected PM conversation receives it while the user continues working. | Durable receipts, deduplication, same-conversation delivery, user interruption preserved. This foundation has real-client evidence. |
| 2. Supervised single-project relay | An authorized person requests a worker status or sends a request in Discord; the PM routes it and returns a correlated reply. | One configured project/channel, authenticated Discord bot adapter, explicit project/worker mapping, owned producer/receiver lifecycle, real round-trip and reconnect tests. Not implemented end to end. |
| 3. Continuous project monitoring | The PM reports a worker failure, a request needing attention, completion, or a missing worker heartbeat; unchanged state stays quiet. | Durable worker/request state, heartbeat expiry as unknown/unreachable rather than invented progress, source recovery, reply delivery tracking, restart and long-running client tests. This is the recommended everyday-use target. |
| 4. Bounded coordination | The PM routes dependent work, requests clarification, and follows up on overdue requests under a configured policy. | Explicit dispatch permissions, request acceptance and completion evidence, retry/cancellation rules, loop and cost budgets, human intervention and audit history. |
| 5. Multiple projects and teams | Separate projects use their own channels, workers and access rules. | Tenant isolation, per-project permissions, fairness, rate limits, operational dashboards and recovery drills. |

Current position: level 1 delivery infrastructure is verified. There are building blocks for level 2 (fixed bindings, source credentials, durable receipts and explicit replies), but no working Discord bridge or managed worker-status collector. This is not yet an unattended Discord PM.

## Proposed first pilot

Use one project, one dedicated Discord channel and two explicitly selected worker conversations. Keep the PM's owning Codex client running on the intended host. Closing the client may preserve queued input but does not provide an always-on PM response.

Start with explicit bot mentions or commands. The adapter handles Discord protocol acknowledgements independently of model response time. Accept only configured users/roles and destinations. Route responses using stored project/channel/request metadata, never a destination supplied by event text. Ignore bot echoes and duplicate source events.

Track worker events such as `started`, `waiting_for_input`, `failed` and `completed`, plus separately observed heartbeat/liveness information. A live process does not prove useful progress. A missing heartbeat is unknown/unreachable until evidence establishes a stronger state. Only relevant transitions and explicit requests wake the PM model; heartbeat collection and status storage happen outside the model.

Keep request states distinct: received by the bridge, queued for Codex, acknowledged by the worker, completed by the worker, and posted back to Discord. Existing queue acceptance or consumption proves none of the later business outcomes. Retain a stable request ID across the route and record uncertain external delivery instead of promising exactly-once posting.

## First implementation priorities

1. Define project/worker/request identities and status events, with timestamps and expiry semantics.
2. Manage a producer's start, status, restart and stop separately from the receiver.
3. Implement a Discord adapter that receives authorized requests and posts explicitly selected replies from the source-scoped outbox.
4. Verify real Discord → same PM conversation → selected worker → correlated Discord reply, alongside ordinary local user input.
5. Test duplicate events, unauthorized routing, bot echo loops, worker silence, receiver/adapter crashes, network loss and client restart before unattended use.

Real-time transport optimization remains useful but is not a prerequisite for a supervised pilot if the current shared-local observation delay is acceptable. It does not replace lifecycle and correctness work. See [latency](LATENCY.md), [TODO](TODO.md) and [verification scope](TESTING.md).
