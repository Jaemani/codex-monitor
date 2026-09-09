# Required owner lifecycle for unattended conversations

Status: unresolved for the default local Desktop path. This is a required functional milestone,
not a limitation that can be closed by adding a warning.

## Acceptance criteria

An explicitly monitored conversation must consume one stored external event and produce its actual
result while its UI is not selected. Returning to that same conversation must preserve normal user
input, drafts and approvals. No event replay, forced competing owner, artificial active turn, held
approval, recurring model prompt or direct store mutation is an acceptable substitute.

## Findings on Codex 0.153.4

- The external queue watcher visits threads in its own `ThreadManager`. Cold stored targets can
  accept input but are not loaded by that write.
- The app-server unload guard observes connection subscribers and active turn/request status. With
  neither, its source uses a 30-minute grace period. Background terminals and goals are not independent
  residency leases. A background terminal is terminated on session shutdown.
- A separate server cannot simply become a permanent substitute owner while preserving normal
  Desktop interaction. An isolated actual two-server probe returned `already has an active writer`
  when the second server tried to resume a persisted fixture held by the first. The fixture used a
  local HTTP error stub; no real model tokens or original incident inputs were used.
- The only confirmed indefinite residency path is a live subscribing connection to the **same**
  app-server owner. Resuming on that owner attaches a subscriber; retaining the connection prevents
  no-subscriber unloading. This is a lifecycle operation that requires explicit setup, not a receipt retry.

Sources in the inspected upstream tag:

- [Queue service](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/queue/src/service.rs)
- [Unload lifecycle](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/request_processors/thread_lifecycle.rs)
- [Thread status](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/thread_status.rs)
- [Writer lock](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/thread-store/src/local/writer_lock.rs)

## Implementation boundary

For CLI, a deliberately shared owner endpoint can support an explicit monitor subscription and the
ordinary `codex --remote` TUI on that same server. Productizing this requires lifecycle supervision,
subscription recovery, approval routing, conflict reporting and real-client restart/interaction tests.

For default local Desktop, the monitor currently has a shared storage writer, not a supported connection
to Desktop's owning server. No verified public endpoint or monitor-residency API has been identified
for that path. A first-party owner subscription/wakeup integration, or an explicitly configured shared
server supported by Desktop, is required before this milestone can pass. Private Desktop IPC, lock
manipulation and periodically creating turns are outside the design.

The original incident remains queued and must be reconciled as the same client message. The diagnosis
and compatibility patch does not implement this missing lifecycle, and must not be described as fixing
the unattended Desktop response failure.
