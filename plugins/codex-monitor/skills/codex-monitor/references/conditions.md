# Conditions and explicit owner endpoints

Use the bundled helper to run `monitor create --help` and check the installed
runtime's options. Reuse the exact conversation, state and existing receiver.

For a file that changes repeatedly, `--debounce SECONDS` waits for a stable
observation before emitting. Restart, failed observations and pause/resume reset
the observed stability window; downtime does not satisfy the threshold.

For a specific JSON condition, identify the pointer, comparison and expected
value from the authorized task. Pass all three together:

```text
monitor create build-failure --thread THREAD_ID --file /absolute/build.json --json-pointer /build/status --operator eq --value '"failed"' --debounce 5
```

The single quotes above are shell quoting; when constructing an argument list,
the value argument is the JSON string `"failed"` without shell quotes. RFC 6901
pointers select keys or array indexes. Operators are eq/ne/gt/gte/lt/lte;
ordering accepts numbers, and equality preserves JSON types.

The first valid sample is a silent baseline. Later stable matched and recovered
states produce events. Unrelated field edits are ignored. Missing fields and
invalid JSON are observation errors, not matches. Inspect `monitor status`
for the condition candidate, last emitted state and errors. Expected and selected
values stay out of public status/events; configuration retains the expected
value locally.

A running receiver must advertise support before new-feature configuration is
stored. If the CLI reports an incompatible receiver, use the installed runtime
to restart the already authorized receiver and verify readiness. Preserve its
state and ownership. Older pre-predicate binaries are unsuitable for that state.

When the user's CLI already runs with `codex --remote ENDPOINT`, use that exact
endpoint with doctor and monitor create. The file is still read on the receiver
host. Direct delivery requires the conversation to be loaded on its owner
server; never create/resume a conversation to make an endpoint check pass.
Keep shared-local as the default when no owner endpoint is provided. A direct
endpoint is an experimental native transport option, not a latency guarantee.
