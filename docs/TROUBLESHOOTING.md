# Intake is not execution

Three independent conditions matter: the external probe or producer is working, the receiver can
write through the required Codex API, and an owning client can consume input for the target conversation.
Receiver ON and binding ON establish neither API compatibility nor an active consumer.

## A health check succeeds but the recovery event is dead

Inspect the original receipt and test the exact endpoint and conversation:

```bash
codex-monitor inspect DELIVERY_ID
codex-monitor doctor --endpoint shared-local --thread THREAD_ID
codex --version
```

A missing `thread/queue/list` method is a protocol incompatibility, not a failed server health check.
Codex 0.153.4 is the tested baseline; use the probe rather than assuming newer versions are compatible.
Update the Codex executable used by the receiver on the affected host and restart that receiver
with the intended runtime/environment. Updating a different terminal's executable is insufficient.
Re-run the probe before relying on the recovery hook. The local POSIX installer is not validated on
Windows; Windows Task Scheduler operation does not establish Codex API support.

Preserve the failed receipt. Inspect it after compatibility is restored and reconcile native acceptance
before choosing any explicit recovery action. Changing versions does not automatically repair a dead
event. An old accepted or uncertain submission must not be blindly replayed.

## Accepted input remains queued

`accepted` means persisted submission. `queued` means the native history has not yet established
consumption of that client message. Neither proves that the target model is running or that it replied.

Inspect the exact target through its owning Codex client. In the tested shared-local implementation,
`notLoaded` tasks do not self-start merely because a queue item was added. Metadata reads do not load
a task. An independent writer's empty loaded-thread list is expected and cannot diagnose Desktop
presence. Compare target IDs, recent turn IDs, OS user and configured Codex storage without modifying them.

A busy or interrupted loaded conversation can also retain queued input. Queue age is useful evidence,
but there is no universal timeout after which replay is safe. Preserve the original receipt and check
for one native consumption and the actual requested result. Any decision to open/load the exact task
must respect its existing workflow; monitoring never silently resumes, starts or interrupts it.

## Operational boundary

For unattended response, provide a supported client/session owner that keeps the intended conversation
loaded. A persistent receiver and durable queue alone cannot provide that owner. A dedicated ordinary
CLI TUI can be operated as such a session; unloaded arbitrary Desktop tasks require an additional
supported lifecycle integration that this project does not currently provide.

External OS health checks can remain quiet and free of model turns while this integration is repaired.
Scheduling model prompts is not a substitute for a missing consumer or unsupported queue API.
