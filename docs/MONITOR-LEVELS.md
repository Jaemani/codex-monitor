# Monitoring technology levels

The primary product object is a monitor attached to the user's existing conversation. Integrations such as Discord are event sources or reply destinations, not the architecture's organizing principle.

| Level | Capability | Current status |
|---|---|---|
| 1. Conversation-targeted delivery | Durable event intake, stable IDs, queue placement, same-conversation consumption and interruption preservation. | Verified baseline on the documented clients. |
| 2. Conversation-scoped management | Create multiple named monitors for the current conversation; list, inspect, pause, resume and remove them without affecting another conversation. | Implemented for local files; scoped process checks and actual ordinary TUI PASS. Managed Desktop event arrival confirmed; pixel/concurrency coverage remains incomplete. |
| 3. Persistent observation and recovery | Run collectors under an owned service lifecycle, report failures, restore checkpoints after restart and distinguish configuration from observed health. | Managed local files now have bounded process isolation, crash/checkpoint recovery and observed health. Installed macOS/Linux failure checks, macOS LaunchAgent lifecycle and a 300-second sampler phase passed. Broader liveness monitoring remains future work. |
| 4. Conditional reactions | Debounce/coalesce relevant changes, sustained-condition thresholds, expiry and explicit escalation policies outside the model. | Durable stable-sample debounce passed deterministic and installed process checks. General predicates and escalation remain future work. |
| 5. Correlated two-way work | Track external requests through dispatch, worker acknowledgement, completion and explicit replies. | Durable source-scoped replies exist; business request lifecycle is not implemented. |
| 6. Independent operation across conversations | Isolate conversation ownership, configuration, permissions, workloads and failures; provide an explicitly requested global overview. | Conversation-scoped registry and two-conversation process isolation PASS; broader resource/tenant isolation is not established. |

## Scope rules

A monitor name is local to its conversation. The same name in two conversations identifies two different monitors. The exact current conversation comes from an explicit thread argument or the host-provided `CODEX_THREAD_ID`; absence is an error, never a reason to pick the newest thread. Global operations require an explicit interface rather than an implicit default.

One conversation may have several monitors. The user can keep typing while monitors run. Management queries do not wake the model. Persisted events may outlive the owning UI, but queue durability does not keep a closed client responding.

## First managed collector

The implementation provides a local file collector with silent initial sampling and unchanged-state suppression. Its registry and checkpoint belong to the monitor state. The existing receiver service owns the collector runtime, so launching a monitor does not leave an untracked detached process. Show desired state, observed collector status, recent sample/error and delivery evidence separately from unknown client/model activity.

Verify two conversations with identical monitor names, changes during active user work, pause/resume/remove isolation, receiver crash and restart, blocked or invalid files, retained pending events and quiet unchanged state. Use actual ordinary TUI for user-visible claims; fake endpoints prove only the specified process/transport behavior.

Near-real-time delivery is a separate latency dimension. Reducing native queue observation delay does not replace durable lifecycle or scoped ownership. See [LATENCY.md](LATENCY.md).
