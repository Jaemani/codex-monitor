# Claude Code comparison: channels, hooks, and agents

Research date: **2026-09-08**. All external links below are first-party Anthropic documentation accessed on that date. Claude Code channels and agent teams are preview/experimental features, so their contracts may change.

Codex implementation update (2026-09-09): conversation-scoped file collectors,
bounded sampler processes, durable debounce and explicit correlated request
state are now implemented. Request lifecycle passed macOS/Linux installed
checks and an actual ordinary TUI flow. Managed delivery through an owned Unix
App Server and `codex --remote` also passed, with one sampled 1.032-second
change-to-visible interval. This does not establish matched-workload superiority,
Desktop interaction parity or a universal latency guarantee. See
[monitoring levels](MONITOR-LEVELS.md) and [latency evidence](LATENCY.md).

## Decision

`codex-monitor` should keep a named binding attached to one existing Codex conversation and present that binding as an **externally monitored conversation**. It should not imitate Claude's channel-specific envelope or create a hidden worker conversation. The current `shared-local` path already has the important continuity property: it stores input for the bound thread ID and lets the CLI or Desktop client that owns that thread consume it.

Claude Code provides three separate ideas that are easy to conflate:

1. **Channels** are native push ingress into a running Claude Code session, optionally paired with an ordinary MCP reply tool.
2. **Hooks** react to Claude Code lifecycle events. A narrowly documented `asyncRewake` completion can wake an idle session, but hooks are not a general durable event transport.
3. **Subagents and agent teams** parallelize work inside or around a session. They are coordination primitives, not a substitute for selecting the user's ongoing conversation.

The product target is therefore: one durable conversation binding, visible external status, provenance-marked inbound messages, explicit replies correlated to the originating event, and normal user input continuing in the same conversation.

## What Anthropic officially supports

### Channels

Anthropic defines a channel as an MCP server that pushes events into the Claude Code session already open. It may be one-way, or two-way when the MCP server also exposes a reply tool. Events arrive only while the session is open; Anthropic recommends a background process or persistent terminal for an always-on setup. Channels are a research preview and require a per-session `--channels` opt-in, in addition to organization policy where applicable. See [Channels](https://code.claude.com/docs/en/channels) and [Channels reference](https://code.claude.com/docs/en/channels-reference).

The UI makes activation and traffic visible:

- startup shows a notice that messages from the selected channel inject into that session;
- inbound traffic renders as a source-labelled line such as `← fakechat · web: ...`;
- a channel reply appears on the external platform, while the terminal shows the reply tool call and a confirmation rather than duplicating the reply text.

These are product-level affordances, not just protocol details. They tell the user which live session receives events and distinguish human prompts from external traffic. See the [Channels quickstart](https://code.claude.com/docs/en/channels#quickstart).

Channel delivery has weaker durability semantics than this repository's inbox. Claude Code does not acknowledge that a notification was processed: the MCP send resolves when it is written to the transport. If the session did not load the server as a channel or policy blocks it, the event can be dropped silently. While Claude is busy, channel events queue in order; multiple notifications may be delivered together on the next turn. Anthropic recommends separate sessions for independent concurrent streams. See [Notification format](https://code.claude.com/docs/en/channels-reference#notification-format).

The documentation specifies ordering among channel events, but it does not specify a total ordering guarantee between a terminal-entered human prompt and an event arriving at the same time. A product built around mixed human and external input should preserve the order it actually observes without claiming a stronger cross-source guarantee.

For human intervention, a two-way channel exposes an ordinary MCP reply tool. A channel may also relay tool permission requests. The local dialog and remote prompt remain live together; the first valid local or remote answer wins. Project trust and MCP-server consent are not relayed. Sender authentication is essential because an allowed remote sender can inject messages and, when permission relay is enabled, approve or deny tool use. See [Expose a reply tool](https://code.claude.com/docs/en/channels-reference#expose-a-reply-tool), [Gate inbound messages](https://code.claude.com/docs/en/channels-reference#gate-inbound-messages), and [Relay permission prompts](https://code.claude.com/docs/en/channels-reference#relay-permission-prompts).

### Hooks and idle wakeups

Hooks run at defined Claude Code lifecycle points such as session start/end, prompt submission, tool use, notifications, agent events, and stop. They do not by themselves define an arbitrary external push channel. See [Hooks reference](https://code.claude.com/docs/en/hooks).

The documented background behavior is precise:

- `async: true` is available only for command hooks. It launches the hook without blocking Claude, and Claude Code does not enforce the configured timeout after it starts. Its `additionalContext` and `systemMessage` are delivered on the next conversation turn; when the session is idle, they wait for the next user interaction.
- `asyncRewake: true` runs a command hook in the background and wakes Claude if that hook exits with code 2. Its stderr, or stdout when stderr is empty, becomes a system reminder. Unlike a plain async hook, its configured or event-specific timeout is still enforced; the common command-hook default is 600 seconds, with lower defaults for some events.
- Hook results are delivered only while the session runs. In `claude -p`, Claude Code kills an unfinished async hook at teardown unless the hook detached its own process.
- Each async firing creates a separate process and has no cross-firing deduplication.

See [Command hook fields](https://code.claude.com/docs/en/hooks#command-hook-fields) and [Run hooks in the background](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background).

The official text supports a **single hook completion waking an idle, still-running session**. It does not document `asyncRewake` as an indefinitely armed listener, durable offline queue, automatic re-registration after exit, or general-purpose webhook ingress. `codex-monitor` should not claim those properties or replace its receiver with a long-running hook.

Notification hooks can alert a human when Claude has been idle for about 60 seconds, needs permission, or when a background session needs input or completes. They observe a Claude Code notification; they do not convert an external event into an ongoing session. Stop hooks can ask Claude to continue, but Claude Code ends the turn after eight consecutive Stop-hook continuations. Stop input exposes registered background tasks and scheduled wakeups so a hook can distinguish completion from waiting. See [Notification](https://code.claude.com/docs/en/hooks#notification) and [Stop](https://code.claude.com/docs/en/hooks#stop).

### Subagents and agent teams

Subagents work within one session, use independent context, and return results to the caller. Background subagents can run while the user continues in the main conversation; permission prompts surface in the main session, and completion reaches the parent as a later notification. A completed named subagent can be resumed with its earlier history, but this remains delegated work under the parent session rather than the product's primary conversation identity. See [Subagents](https://code.claude.com/docs/en/sub-agents) and [foreground/background behavior](https://code.claude.com/docs/en/sub-agents#run-subagents-in-foreground-or-background).

Current documented subagent limits are:

- default maximum **20 concurrently running subagents** per session, configurable with `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`; there is no total lifetime count limit;
- default nesting depth **three subagent layers below the main conversation**, configurable with `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`;
- user-started in-session forks and resumed completed subagents can exceed the concurrency gate in the documented cases, while workflows and agent-team teammates use separate limits.
- a separate `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` setting limits concurrent read-only tools and subagents, with a documented default of **10**.

See [Concurrent subagent limit](https://code.claude.com/docs/en/sub-agents#concurrent-subagent-limit), [nested subagents](https://code.claude.com/docs/en/sub-agents#let-subagents-spawn-their-own-subagents), and [environment variables](https://code.claude.com/docs/en/env-vars).

Agent teams are experimental and disabled by default. A team consists of a lead session, independent teammate sessions, a shared task list, and file-backed mailboxes. Teammates communicate directly, and the user can open any teammate transcript and send it messages without routing through the lead. Agent-to-agent messages are explicitly not user approval. See [Agent teams](https://code.claude.com/docs/en/agent-teams), [Talk to teammates directly](https://code.claude.com/docs/en/agent-teams#talk-to-teammates-directly), and [Messages between agents](https://code.claude.com/docs/en/agent-teams#messages-between-agents).

Anthropic documents no hard teammate-count limit and recommends starting with three to five because token cost and coordination overhead scale with team size. Teams also have material lifecycle limits: one team per session, no nested teams, no restoration of in-process teammates on resume, and a fixed lead. See [Choose an appropriate team size](https://code.claude.com/docs/en/agent-teams#choose-an-appropriate-team-size) and [Agent-team limitations](https://code.claude.com/docs/en/agent-teams#limitations).

## Comparison with the current product

| Requirement | Claude Code | `codex-monitor` working tree | Consequence |
|---|---|---|---|
| Keep one ongoing conversation | Channel injects into the currently running, explicitly opted-in session. It stops receiving when that session closes. | `bind`/`attach` records a fixed Codex thread ID. `shared-local` can store queued input while the owning client is closed and the same thread can consume it after reopening. | Keep the fixed binding. Never select a new thread automatically and never describe a writer process as the user's session. |
| Make monitoring visible | Startup channel notice, source-labelled inbound line, `/mcp` status. | [`sessions`](../codex_monitor/sessions.py) reports receiver state, binding state, thread ID, allowed sources, event counts, and the latest event without waking Codex. [`render_event`](../codex_monitor/presentation.py) includes source, type, binding, and receipt in the visible queued message. | Treat this external companion view as the status surface. Do not claim native Codex UI integration or a hidden input path. |
| Receive while the model is busy | Events queue in order and may be grouped on the next turn. | [`Monitor`](../codex_monitor/monitor.py) persists input, deduplicates it, applies per-binding FIFO, and sends through the official Codex queue. | Preserve event IDs and ordering. UI copy must distinguish inbox acceptance, native queue consumption, and model completion. |
| Receive while the UI is closed | Not supported by channels; the session must remain open. | The repository's validated `shared-local` queue can accept for an existing thread while its client is closed. | This is a useful difference. Report the client as **unknown/offline-unverified**, not connected or awake. |
| Let the local human keep working | The terminal user can keep typing in the same session; channels inject alongside that work. | The owner keeps using the bound CLI/Desktop thread. Queue input does not start/resume the thread or interrupt the current turn. | Preserve user input, draft, interrupt, and approval behavior; external data never grants permission. |
| Send a response to the source | Two-way channel uses an MCP reply tool and routes by channel metadata. | [`reply`](../codex_monitor/cli.py) writes a deliberate response correlated to one delivery. [`ReplyStore`](../codex_monitor/replies.py) lets only the originating source pull and acknowledge it. | Keep replies explicit and idempotent. Do not scrape arbitrary assistant text or auto-forward every model response. |
| Human intervention from outside | A two-way channel can send chat messages; optional permission relay races local and remote answers. | An authenticated source can send another event, and the local user can type in the same Codex thread. There is no supported remote approval relay. | This satisfies conversational intervention. Keep approval/consent out of event payloads until Codex exposes a supported permission-relay contract. |
| Long-lived wake mechanism | Channel requires an open process. A single `asyncRewake` hook completion can wake an idle running Claude session. | The receiver is a durable OS service; Codex's native queue consumer decides when the bound thread processes input. | Retain the service and queue architecture. Do not advertise an indefinite hook-like wake guarantee. |
| Parallel agents | Subagents and teams have separate contexts, panels, messaging, and documented limits. | Agent-to-agent traffic is just another authenticated event source with hop and trace budgets. | Do not copy Claude team mailboxes or team lifecycle into the monitor. Keep agent identity and authority explicit at ingress. |

The current Codex route is a queued user-message path. No supported native mechanism has been confirmed for injecting a hidden channel event or displaying a persistent monitor badge inside Codex CLI/Desktop. The safe product representation is a visible provenance-marked message plus an external `sessions` view.

## Implemented alignment and remaining recommendations

The working tree now implements the core session UX in [`SESSION-WORKFLOW.md`](SESSION-WORKFLOW.md): `attach` for a fixed conversation, `sessions` for an external status view, `pause`/`unpause` for the binding, a human-readable event renderer, and an explicit durable reply outbox. These choices align with the supported Claude behaviors without pretending Codex has a native Channels surface.

### P0: keep the attached conversation as the primary product object

Use `attach NAME --thread THREAD --source SOURCE` in user-facing examples and call the result a monitored conversation. Show the immutable thread ID at attach time and in every status view. Keep reassignment explicit by requiring a new binding or an explicit migration flow; silent rebinding would violate the user's expectation that one particular conversation is ongoing.

The status model should keep these facts separate:

- receiver process: running or stopped;
- binding: enabled or paused;
- configured thread ID and allowed sources;
- inbox counts and latest receipt;
- native evidence for a selected delivery: queued, consumed, or unknown;
- client presence, model activity, and source health: unknown unless directly measured.

[`sessions`](../codex_monitor/sessions.py) implements the core external view, and [`SESSION-WORKFLOW.md`](SESSION-WORKFLOW.md) documents it next to `attach`, `pause`, and `unpause`. Keep its note that receiver state does not establish source health or model activity whenever this workflow is summarized elsewhere.

### P0: preserve human-readable provenance in the conversation

Every inbound item should visibly state its source, event type, binding, and receipt before showing data. [`render_event`](../codex_monitor/presentation.py) does this and sanitizes terminal-control and bidirectional-control characters. Retain the explicit “untrusted data” boundary. It is the Codex equivalent of Claude's source-labelled channel line, within the capability actually available here.

Do not prepend text that implies the external sender is the user, system, administrator, or approver. An external message may provide facts or a request, but existing user instructions and tool permissions remain authoritative.

### P0: retain and publish the explicit two-way contract

The reply outbox should remain opt-in and correlated to the inbound delivery. The source-facing contract should document:

1. send an event with a stable source event ID;
2. keep the returned delivery ID;
3. a human or agent explicitly runs `reply DELIVERY_ID --id STABLE_REPLY_ID --message ...` in the monitored environment;
4. the original source polls its authenticated reply endpoint and acknowledges each reply ID;
5. retries reuse the same reply ID and content.

This supports send/receive alongside local human messages without guessing which assistant paragraph is a response. The current reply and session tests cover source isolation, retry idempotency, repeated acknowledgement, and durable re-open behavior. Keep those cases in the release gate and ensure the top-level README points external source authors to the full contract in [`SESSION-WORKFLOW.md`](SESSION-WORKFLOW.md).

### P1: expose operational state without creating turns

Provide a compact watchable status mode or a small loopback-only status page if operators need a persistent indicator. It should read local state and native delivery evidence only. It must not inject health prompts, poll the model, start/resume a conversation, or convert `accepted`/`consumed` into “completed.”

An eventual native badge would require a supported Codex extension point. Until one exists, external status is the honest design.

### P1: keep remote approvals outside this transport

Claude's permission relay is a dedicated, authenticated protocol with one-time request IDs and local/remote first-answer semantics. A normal channel message cannot approve a tool call. `codex-monitor` should likewise reject any attempt to encode approval as ordinary event data. If Codex later publishes a permission relay API, implement it as a separate capability with distinct credentials, expiring request IDs, audit records, and an always-available local decision path.

### P2: use agents as event producers, not session owners

Keep `trace_id`, `hops`, per-trace budgets, source credentials, and explicit target bindings for agent-originated traffic. A subagent or team can send findings into the monitored conversation, but it must not choose a replacement conversation, inherit user authority, or acknowledge its own requests in a loop. Claude's own team rules support the same boundary: agent messages are not user approval.

Avoid adopting Claude agent-team assumptions such as one team per session, file mailboxes, or teammate resume behavior. They are experimental orchestration details and do not improve the core promise of one durable user conversation.

## Acceptance criteria for the session UX

The product can claim the intended experience when all of the following are demonstrated:

- `sessions NAME` identifies the same thread before and after receiver and client restarts.
- Pausing the binding is visible externally and rejects or holds new ingress according to a documented rule; it does not alter already accepted Codex queue entries.
- A local human message before and after an external event remains in the same thread, with the external event visibly labelled and delivered once.
- An event arriving during a model turn waits without interrupting it; an event arriving while the UI is closed remains durable and is later consumed by the same thread.
- A reply is visible only to its originating authenticated source, survives receiver restart, and is idempotent under retry and acknowledgement loss.
- Status never equates receiver liveness with client presence, queue acceptance with consumption, or consumption with successful model work.
- External events and agent messages cannot approve tools, change permissions, or override a user interrupt.
