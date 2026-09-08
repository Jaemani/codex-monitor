# Compatibility, reliability, and usability assessment

Assessment date: **2026-09-08**. The comparison covers Claude Code's documented Channels, `asyncRewake` hooks, subagents, and agent teams. Claude behavior comes from [first-party documentation research](CLAUDE-COMPARISON.md); codex-monitor behavior comes from saved execution evidence. The two products have not undergone the same large-scale fault-injection campaign, so this document does not claim an overall reliability advantage.

## Current conclusion

The core flow is implemented and verified: one selected conversation can continue to receive both human input and external events. codex-monitor is not equal to or better than Claude Code in every area.

| Criterion | codex-monitor evidence | Assessment |
|---|---|---|
| Human input and events in one conversation | Real macOS/Linux TUI, remote TUI, Desktop native consumption, and user-observed Desktop restart | Core flow met |
| Retention while the app is closed | Event stored in the official `shared-local` queue, then consumed by the same Desktop or CLI thread | Useful behavior beyond Claude Channels' documented open-session requirement |
| Receipt, duplicate, and uncertainty tracking | Durable inbox, stable IDs, `inspect`, and per-source reply outbox with acknowledgement | Strong diagnostics; does not guarantee exactly-once business side effects |
| Long-running behavior | 3,600-second TUI soak and 3,600-second receiver fault run with 55 restarts; failure records retained | Bounded real-world validation passed; no overall superiority claim |
| Local intervention and permissions | Draft and active-turn concurrency plus TUI recovery after Ctrl+C and a new user prompt | Core behavior met; Desktop draft and approval cases remain |
| CLI and Desktop support | Verified macOS/Linux TUI combinations and the current Desktop app | Other Windows, WSL, IDE, and SSH combinations remain unverified |
| Native UI integration | External `sessions` status and readable queued event messages | Less integrated than Claude's native channel indicator and reply tools |
| Event latency | Current Codex `shared-local` consumer checks external queue changes about every 10 seconds | No latency parity with Claude MCP push; immediate response is not guaranteed |
| Conversation management | `$codex-monitor` skill plus `attach`, `sessions`, `pause`, and `reply` | Easier management; installing the skill does not start the runtime or a producer |
| Continuous producer management | Receiver service exists; `watch-file` still needs its own process supervision | Usability gap; an enabled binding does not imply that a producer is running |
| Replies to external sources | Originating source pulls explicit replies and acknowledges them | Two-way delivery exists, without a messenger-native reply UI |
| Remote approval | External events cannot grant permissions | Intentionally absent; not feature-equivalent to Claude permission relay |
| Agent-team orchestration | Source, trace, and hop-based messaging only | Does not replace Claude team panels, task coordination, or mailboxes |

## Deployment and management

The skill teaches conversation-level operations, the runtime receives and stores events, and the OS service keeps the receiver running. Runtime and skill installation can share one installer command, while choosing what to monitor remains explicit.

An upgrade stages and validates a new environment before switching the stable executable. It never upgrades the interpreter used by an installed service in place. A failed candidate must leave the current release selected. Uninstall preserves monitor state, tokens, receipts, user-modified skill files, and unrelated data. See [INSTALLATION.md](INSTALLATION.md) and the installer tests.

The current release is a local artifact. It is not published to a package registry or marketplace. Plugin and skill format validation is distinct from discovery and execution on every Codex host. Installing the local wheel may contact the configured Python index for third-party dependencies. The installer currently supports macOS and Linux and does not claim Windows support.

The independent audit observed Claude Code 2.1.220 and performed only read-only `--version` and `--help` checks. It did not start a Claude Channels session or inject the same events and failures into Claude. The assessment therefore compares Claude's documented contract with codex-monitor's execution evidence. See [the audit evidence index](evidence/README.md).

## Next priorities

1. Manage receiver and producer status together so a natural-language start request can lead to a supervised, durable watch.
2. Validate skill installation, discovery, start, and stop in a clean new-user environment.
3. Build selected external-system adapters that combine signature verification, relevant-event filtering, and reply retrieval.
4. Test Windows/WSL and the remaining Desktop draft and approval concurrency cases.

Core event delivery, installation, producer supervision, and native UI integration have separate acceptance criteria. Passing one does not establish product-wide superiority.

## Evidence required for a stronger parity claim

Connect the same producer and event set to both products and record:

| Scenario | Compare |
|---|---|
| New install, start monitoring, inspect status, uninstall | Human steps, recovery from errors, preservation of settings and credentials |
| Event during idle, response, and draft entry | Same conversation, draft preservation, p50/p95 latency, duplicates, loss |
| Event during interruption or permission prompt | Respect for the user's decision and recovery after normal user input |
| Client, receiver, and producer restart | Storage boundary, recovery, and truthful status |
| Burst, duplicate, and temporary network failure | Ordering, suppression, uncertainty, and retry behavior |
| Explicit reply, lost response, and retry | Correct recipient, persistence, deduplication, and required user actions |

Claude Channels does not document delivery while its session is closed. Any comparison of that case must say whether an additional adapter was used and must not attribute adapter behavior to the base feature.
