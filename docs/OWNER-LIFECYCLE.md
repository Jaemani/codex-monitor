# Required owner lifecycle for unattended conversations

Development priority: CLI on an explicitly shared owner. The default local Desktop path remains
unresolved. CLI implementation and evidence must not be presented as a Desktop fix.

## Request from Desktop, run through CLI

You can ask a Desktop assistant with local shell access to set up CLI-backed monitoring. The place
where you ask is separate from the client that owns and runs the monitored conversation. This needs
no remote computer or SSH: the CLI owner can run on the same machine over a loopback endpoint.

| Arrangement | What works | Limit |
|---|---|---|
| Loaded local Desktop conversation + shared-local | Events enter the existing conversation and the user continues in Desktop | Queue writes do not keep it loaded or wake an unloaded task |
| Ordinary CLI TUI alone | Events can be consumed while that client owns the conversation | Closing the owner can end availability |
| CLI owner + resident + receiver | Selected conversations remain subscribed after the TUI closes; the user can reconnect through that same owner | All required processes and the host must stay available; approvals can still pause work |
| Request in Desktop, execution in CLI | A Desktop assistant can configure the preceding CLI arrangement and explain how to connect | This does not turn Desktop into a client of that CLI owner or make the requesting Desktop chat resident |

The CLI owner exposes an endpoint we deliberately configure. Desktop manages its own internal server;
no verified public attachment endpoint for that existing local owner has been identified. Sharing a
queue store does not grant access to its conversation lifecycle. This is the current integration
boundary, not proof that Desktop support is technically impossible in a future version. See
[compatibility and verification limits](COMPATIBILITY.md).

Choose the exact target before setup. A separate CLI monitoring conversation leaves the Desktop
requesting conversation available for user discussion. Reusing an existing Desktop conversation ID
requires a deliberate ownership transition: preserve history and queued input, and resolve any active
writer conflict before registering it with the CLI owner. Simultaneous interactive ownership of that
same task in independent Desktop and CLI servers is not supported by this workflow. A CLI worker's
responses stay in its task; delivery back to a separate Desktop task requires explicit relay setup.

Example request from Desktop:

> Use $codex-monitor to monitor this project's server. Configure a local CLI owner and resident for
> the monitoring task. Keep health checks outside the model and deliver an event only on failure or
> recovery. Group it under this project. Tell me which conversation runs it, how to open its TUI, how
> to check monitoring health, and how to stop it. Do not use recurring model prompts.

An external server probe or producer must actually be installed and verified; the receiver alone
cannot detect outages. A timer in an OS supervisor may run lightweight health checks without model
turns. Recovery actions require the user's authorization and remain subject to native approvals.

## What to tell the user after setup

Provide the actual project, conversation name/ID, endpoint and watched condition, followed by copyable
commands using those values. Report unverified components explicitly rather than saying only "running."

| User action | Command or observation |
|---|---|
| View registered conversations and routes | `codex-monitor dashboard` in an interactive terminal |
| Get a one-time snapshot | `codex-monitor dashboard --once --json` |
| Enter the monitoring conversation | `codex-monitor connect --endpoint ENDPOINT --thread THREAD_ID --cwd /absolute/project` |
| Check a managed collector | `codex-monitor monitor status NAME --thread THREAD_ID` |
| Check an external binding | `codex-monitor sessions BINDING` |
| Confirm a particular delivery | `codex-monitor inspect DELIVERY_ID`; check native consumption and the actual requested result separately |
| Stop/resume/remove a route in the dashboard | Select the conversation and exact route with Tab; use `p`, `r`, or `x` and confirm removal with `y` |

Dashboard dots indicate the documented configuration/component state; a green binding does not prove
that its producer is healthy or the target is consuming events. Check the producer/collector, receiver,
owner/resident subscription, and a controlled end-to-end event. No native Desktop monitoring badge is
promised. Unchanged monitoring does not need chat status messages or model polling.

Stopping a route leaves the shared owner, receiver and other routes running. It cannot retract input
already accepted by Codex. Resuming a route does not restart an external producer or load a Desktop
task. Removing a route preserves conversation history and receipts. See [dashboard controls](DASHBOARD.md).

## CLI resident setup

Use a validated Codex build (currently 0.153.4). All three processes must use the same owner endpoint
and the owner must retain its Codex storage across restarts. `resident` registers only the exact
existing conversation IDs you provide. Registration resumes/subscribes to those conversations;
any already queued input can consequently start native processing. It does not create a new task.

In one terminal, run the owner:

```bash
codex-monitor host --port 8765
```

In another, open the ordinary TUI and create or choose the intended conversation:

```bash
codex-monitor connect --endpoint ws://127.0.0.1:8765 --cwd /absolute/project
```

Set `THREAD_ID` to that exact conversation ID. In initialized receiver state, assign its project and
stable role name, then verify them:

```bash
codex-monitor conversation set --thread "$THREAD_ID" --project "My project" --name "Server monitor"
codex-monitor conversation list --thread "$THREAD_ID"
```

In a third terminal, retain its subscription:

```bash
codex-monitor resident --endpoint ws://127.0.0.1:8765 --thread "$THREAD_ID"
```

Repeat `--thread` for additional explicitly chosen conversations. The resident prints connection and
subscription state changes without sending chat messages. Its periodic owner probes do not invoke a
model. `subscribed`/`ready` describes owner availability, not completed work or absence of an approval.

The resident requests metadata and live resume state with `excludeTurns: true`; it does not download
the full conversation history to retain a subscription. This leaves persisted history intact and
avoids an unnecessary large resume response. It does not remove transport limits on other responses
or live notifications.

Configure the receiver binding or managed monitor with this same `--endpoint`. Existing shared-local
bindings are not silently migrated. For a new file monitor in initialized receiver state:

```bash
codex-monitor monitor create build --thread "$THREAD_ID" \
  --file /absolute/project/build-status.json --endpoint ws://127.0.0.1:8765
codex-monitor serve
```

The receiver is separate from the owner and resident. An already running receiver can be reused.
Return to the same conversation with:

```bash
codex-monitor connect --endpoint ws://127.0.0.1:8765 --cwd /absolute/project --thread "$THREAD_ID"
```

Do not open a second independent local server to resume a task held by this owner. Use `--remote`
against the existing owner. `shared-local` is rejected by `resident` because its independent writer
is not the interactive owner.

## What reconnect restores

| Failure | Recovery | Boundary |
|---|---|---|
| Resident RPC connection breaks | Reconnect to the configured endpoint with bounded backoff and restore explicit subscriptions | No new queue item or replacement task |
| One registration temporarily fails | Retry that target with bounded backoff while retaining healthy subscriptions | Stable invalid-target, missing-method and ownership errors require correction |
| Owner App Server restarts | Resident waits for that endpoint to return, checks compatibility and registers the same IDs | The owner process itself needs a supervisor or manual restart, with persistent storage |
| Resident process exits | Restart the same command, with the same explicit IDs | Registrations currently live in command arguments; receiver `service install` does not supervise the resident |
| TUI closes | Resident retains subscriptions while both resident and owner remain running | No human is present to answer native approval or input requests |
| Receiver disconnects or restarts | Durable inbox delivery/reconciliation resumes | Accepted or uncertain input is not blindly replayed |
| Another server owns the task | Report the per-task conflict | No lock stealing or forced competing owner |
| Model turn is interrupted by a server crash | Inspect native history and actual work state | Reconnection is not proof of successful completion and does not automatically repeat side effects |

For unattended operation, supervise the owner, resident and receiver as separate OS processes.
Keep the endpoint, owner storage and explicit thread arguments stable. The built-in macOS `service`
command currently manages only the receiver; closing a foreground owner/resident terminal ends that
process. Native permissions continue to apply. Reopen the TUI to handle approvals; the resident
neither grants nor rejects them.

Known pre-submission connection failures wait without spending the submission-attempt budget.
Pending events still expire under the configured `max_age` (default one hour); expired events remain
inspectable as `dead` and require an explicit operator decision. Actual retryable submission
rejections retain their bounded attempt budget. Reconnection does not automatically revive dead
events from a prior deployment.

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

The inspected App Server source also retains pending server requests and replays them to a connection
when it resumes that thread: see
[outgoing requests](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/outgoing_message.rs)
and the resume path in the lifecycle source above. The resident sends no approval or elicitation
response, allowing the native TUI to answer. This source finding is not a real approval-screen test,
and does not prove pending approvals survive an owner process crash.

## Implementation boundary

For CLI, a deliberately shared owner endpoint can support an explicit monitor subscription and the
ordinary `codex --remote` TUI on that same server. The foreground resident now implements explicit
subscription recovery and per-task conflict reporting. Operational completion still requires
lifecycle supervision and the remaining real-client approval, restart and endurance tests.

For default local Desktop, the monitor currently has a shared storage writer, not a supported connection
to Desktop's owning server. No verified public endpoint or monitor-residency API has been identified
for that path. A first-party owner subscription/wakeup integration, or an explicitly configured shared
server supported by Desktop, is required before this milestone can pass. Private Desktop IPC, lock
manipulation and periodically creating turns are outside the design.

The original incident remains queued and must be reconciled as the same client message. The diagnosis
and compatibility patch does not implement this missing lifecycle, and must not be described as fixing
the unattended Desktop response failure.
