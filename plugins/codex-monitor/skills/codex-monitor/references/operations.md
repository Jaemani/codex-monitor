# Receiver and producers

Use the same `--state PATH` throughout. `init` is for new state only. On macOS use `service install`,
`service status`, and `sessions`. Verify `/v1/status` on the configured loopback port with the admin
token read directly from its file; never print token contents. Service registration and process locks
do not prove HTTP health. Adding a source requires receiver restart to reload credentials.

For “is monitoring working?”, inspect the requested binding with `sessions NAME`, and report
configuration, receiver process, producer observation, and delivery evidence separately. For a managed
collector use `monitor status NAME --thread THREAD_ID` instead. External producer health remains
unknown until checked through that producer's own supported health interface; a successful old event
or Discord REST authentication does not prove a live Gateway connection. Inspect the latest receipt
when delivery diagnosis is requested. Native consumption proves input processing, not task completion
or a reply reaching its destination. State the missing check rather than calling everything healthy.

A CI/server-hook adapter posts `id`, `source`, `type`, `data` to `POST /v1/events/SESSION_NAME` with its
source bearer token. Prefer `data.message` for readable content. Keep stable IDs, authenticate upstream
events, and filter unchanged/non-actionable state. The receiver does not verify arbitrary vendor
signatures. 202 means durable inbox receipt, not model completion.

For a local file, prefer `monitor create NAME --thread THREAD_ID --file /absolute/status.json`.
The receiver owns this managed collector; initial and unchanged samples are silent. Use scoped
`monitor status/pause/resume/remove` commands. The legacy `watch-file` command remains a separate
foreground producer and is not adopted by the managed registry. File contents are not sent, only
change metadata. Managed collectors currently use shared-local; remote collector placement is not
implemented.

Agent traffic can use `send --to NAME --source SOURCE --id STABLE_ID --trace TRACE --hops N --type
agent.message --data -` with finite JSON on stdin. Preserve trace/hops and identity. `agent.ack` never
wakes the model; do not create an automatic echo loop.

`service stop` stops the receiver; `pause NAME` controls only one binding. `service uninstall` preserves
configuration and receipts. Resolve dead/uncertain deliveries only after inspection and an explicit
decision; unknown native state is not proof that replay is safe.
