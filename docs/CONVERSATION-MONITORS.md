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

Use an explicit `--thread` or exactly `CODEX_THREAD_ID` from the host. If both are present they must match; a mismatch is rejected to prevent operating on another conversation. Missing scope is an error; it never selects the latest conversation or silently operates globally. The default endpoint is `shared-local`.

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

Sampling accepts regular local files only, rejects special files without waiting for a FIFO writer, and bounds hashing to 8 MiB per file with a 0.25-second inter-read budget. The final file component is not followed as a symlink on platforms supporting that open flag. The supported interval is 0.1–86,400 seconds, default 2 seconds. Limits are 32 retained monitor definitions per conversation and 128 per state directory; paused definitions count until removed. Sampling uses bounded child processes. Parent deadlines and per-conversation limits are described in [reliability limits](RELIABILITY-LIMITS.md). An uninterruptible kernel read may outlive a kill request, so this is not an unconditional throughput or host-failure guarantee.

## Debounce repeated changes

```bash
codex-monitor monitor create build --thread "$THREAD_ID" \
  --file /absolute/project/build-status.json --interval 1 --debounce 5
```

`--debounce` defaults to zero and accepts 0–86,400 seconds. With a positive value, the monitor waits until observed samples remain equal for that duration before emitting a changed state. Returning to the last emitted sample cancels a pending candidate. The initial baseline remains silent. This is a stable-sample filter, not an arbitrary predicate or a guarantee that every intermediate file write was observed.

`monitor status` reports the condition candidate separately from the last emitted sample and delivery receipt. Condition timing uses a monotonic clock; saved readings are not UTC timestamps. Restart preserves the candidate but begins its stability window again after the next observation. Pause/resume and failed observations also invalidate prior timing, so downtime is not credited as healthy observation. A candidate becomes committed only after the durable watcher handles it; an already pending event is recovered before evaluating a newer candidate.

## JSON conditions

For a file such as `{"build":{"status":"ok"},"updated":17}`, select a
condition explicitly:

```bash
codex-monitor monitor create build-failure --thread "$THREAD_ID" \
  --file /absolute/project/build-status.json --interval 1 \
  --json-pointer /build/status --operator eq --value '"failed"' --debounce 5
```

All three predicate arguments are required together. The pointer uses RFC 6901:
`/` separates keys, `~1` represents a slash in a key and `~0` a tilde. An empty
pointer selects the whole document. Array indexes use ASCII decimal digits.
Operators are `eq`, `ne`, `gt`, `gte`, `lt` and `lte`. Equality preserves JSON
types, so `true` differs from `1`; ordering accepts finite numbers only.

The first valid observation is a silent baseline, even if already matched.
Subsequent stable transitions to matched or not matched emit one event. Unrelated
field changes do not reset the stability window. Missing fields, malformed JSON,
non-finite values and failed reads invalidate observation rather than making
`ne` true. Recovery starts a fresh observed stability window. A saved pending
event remains recoverable even if newer file content becomes invalid.

Evaluation runs in the bounded sampler process. Only derived condition state is
checkpointed or emitted; status exposes the pointer and operator, not selected
or expected values. The expected value is retained in the private local monitor
configuration so the receiver can evaluate it after restart. Pointer limits are
1,024 UTF-8 bytes and 32 segments; expected JSON is capped at 4 KiB. The file
read size and worker deadline limits also apply.

If a receiver is running, predicate creation checks its advertised capability
before storing the watch. An older or unresponsive receiver must be restarted
with a compatible runtime; it must not silently treat a predicate as a hash
watch. With no receiver running, offline creation remains available. Running
older pre-predicate binaries against a state directory containing predicates
is unsupported; preserve a matching backup before downgrading.

## Existing commands and owner endpoints

An existing CLI TUI connected with `codex --remote` can use the same explicit
App Server endpoint for its managed monitor:

```bash
codex-monitor doctor --surface cli --endpoint "$ENDPOINT" --thread "$THREAD_ID"
codex-monitor monitor create build --thread "$THREAD_ID" \
  --endpoint "$ENDPOINT" --file /absolute/project/build-status.json
```

The configured endpoint is shown in monitor status. Endpoint validation is
purely local, so an offline definition can be stored. Predicate creation also
checks a running receiver's capability as described above.
Delivery requires that exact conversation to be loaded on the selected server.
The monitor never loads or resumes it. A remote endpoint changes delivery only;
the watched file remains local to the receiver host.

Supported adapter forms are `shared-local`, `local`, `unix://`,
`unix:///absolute/socket`, `ssh://ALIAS`, loopback `ws://`, and `wss://`.
Use an existing trusted host alias for SSH. Bearer credentials belong in
`CODEX_MONITOR_SERVER_TOKEN_FILE`, not URL user information. The native
[App Server transport](https://learn.chatgpt.com/docs/app-server#connect-the-cli-terminal-ui)
is experimental; using an official API does not establish upstream production
support or parity across every client. See STATUS.md for tested combinations.

`watch-file` remains a separate foreground producer. It is not automatically adopted by the managed registry. `attach`, `sessions` and binding `pause`/`unpause` remain available for external webhook/agent sources. Prefer the scoped `monitor` commands for a managed collector.

A native background badge and hidden channel input are not provided. Managed file events have no external reply recipient; respond in the conversation. Explicit source-scoped replies remain available for external producers. The user's ordinary CLI/Desktop conversation remains the interaction surface. Default shared queue observation latency is unchanged; direct-owner latency must be measured separately. See [LATENCY.md](LATENCY.md). Verification and remaining limits are recorded in [STATUS.md](STATUS.md).
