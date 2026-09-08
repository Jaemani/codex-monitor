# Conversation-scoped file monitors

A managed monitor observes one explicitly selected local file and routes meaningful changes to one existing Codex conversation. Multiple monitors can share a conversation. The same name in different conversations identifies different monitors.

The examples use `codex-monitor` on PATH. Otherwise use the absolute launcher path printed by the installer. Use the same `--state` directory throughout.

## Create and run

Initialize a new state once, then inspect the exact target:

```bash
codex-monitor init
codex-monitor doctor --thread "$THREAD_ID"
codex-monitor monitor create build --thread "$THREAD_ID" --file /absolute/project/build-status.json
```

Use an explicit `--thread` or exactly `CODEX_THREAD_ID` from the host. If both are present they must match; a mismatch is rejected to prevent operating on another conversation. Missing scope is an error; it never selects the latest conversation or silently operates globally. Managed collectors currently require `shared-local`.

Creating a monitor persists its definition. The receiver must be running to sample files:

```bash
codex-monitor serve
```

For persistent macOS operation, use a wheel-installed runtime and `service install` as described in [operations](OPERATIONS.md). The receiver owns the managed collector runtime and restores its checkpoints on restart. No separate source registration, source token or detached file-watcher command is required. A foreground receiver lasts only as long as its process.

Check readiness from another terminal:

```bash
codex-monitor monitor list --thread "$THREAD_ID"
codex-monitor monitor status build --thread "$THREAD_ID"
```

Output is JSON. It separates desired `enabled`, `receiver_running`, observed `collector_status`, the last checkpoint sample, sampling/delivery errors and the last delivery receipt. Target client availability and target verification remain unknown/not checked in this status view. Use `doctor`, `inspect` and actual client observations for their respective evidence; a collector running does not mean a model task succeeded.

## Pause, resume and remove

```bash
codex-monitor monitor pause build --thread "$THREAD_ID"
codex-monitor monitor resume build --thread "$THREAD_ID"
codex-monitor monitor remove build --thread "$THREAD_ID"
```

Only that conversation's monitor is affected. Other monitors and the shared receiver remain running. Pause disables the associated binding as well as sampling. Resume continues from the persisted checkpoint, so changes since the last saved sample can produce a new event. It is not an audit log of every write during downtime.

Removal stops future collection and disables the old binding while retaining receipts and checkpoint evidence. Recreating the name makes a new monitor generation with a new binding; old pending events are not silently adopted by the replacement. Already accepted native input is not deleted. An in-flight submission may finish after pause or removal.

## Observation contract

The first sample establishes a silent baseline. Unchanged samples produce no model input. Changes in content hash, existence or persistent readability state are represented as events; file contents are not sent. Temporary reads during an in-place write may be retried rather than reported as stable changes.

Pending event IDs and samples are checkpointed before intake. Retries preserve event IDs so acknowledgement loss does not require blind duplication. Local receipts and native queue delivery are separate from client consumption and completed work.

Sampling accepts regular local files only, rejects special files without waiting for a FIFO writer, and bounds hashing to 8 MiB per file with a 0.25-second inter-read budget. The final file component is not followed as a symlink on platforms supporting that open flag. The supported interval is 0.1–86,400 seconds, default 2 seconds. Limits are 32 retained monitor definitions per conversation and 128 per state directory; paused definitions count until removed. Sampling is sequential. An individual OS file read cannot be forcibly interrupted, so these limits are not a throughput or network-filesystem latency guarantee.

## Compatibility with existing commands

`watch-file` remains a separate foreground producer. It is not automatically adopted by the managed registry. `attach`, `sessions` and binding `pause`/`unpause` remain available for external webhook/agent sources. Prefer the scoped `monitor` commands for a managed collector.

A native background badge and hidden channel input are not provided. Managed file events have no external reply recipient; respond in the conversation. Explicit source-scoped replies remain available for external producers. The user's ordinary CLI/Desktop conversation remains the interaction surface. Shared queue observation latency is unchanged; see [LATENCY.md](LATENCY.md). Verification and remaining limits are recorded in [STATUS.md](STATUS.md).
