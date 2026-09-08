# Receiver and producers

Use the same `--state PATH` throughout. `init` is for new state only. On macOS use `service install`,
`service status`, and `sessions`. Verify `/v1/status` on the configured loopback port with the admin
token read directly from its file; never print token contents. Service registration and process locks
do not prove HTTP health. Adding a source requires receiver restart to reload credentials.

A CI/server-hook adapter posts `id`, `source`, `type`, `data` to `POST /v1/events/SESSION_NAME` with its
source bearer token. Prefer `data.message` for readable content. Keep stable IDs, authenticate upstream
events, and filter unchanged/non-actionable state. The receiver does not verify arbitrary vendor
signatures. 202 means durable inbox receipt, not model completion.

For a file use `watch-file /absolute/status.json --to work --source health`. This is a separate foreground
producer: the first and unchanged samples are silent. A receiver service does not supervise that producer.
Use an available authorized supervisor for a persistent producer, or report the temporary process and
how to stop it. File contents are not sent, only change metadata.

Agent traffic can use `send --to NAME --source SOURCE --id STABLE_ID --trace TRACE --hops N --type
agent.message --data -` with finite JSON on stdin. Preserve trace/hops and identity. `agent.ack` never
wakes the model; do not create an automatic echo loop.

`service stop` stops the receiver; `pause NAME` controls only one binding. `service uninstall` preserves
configuration and receipts. Resolve dead/uncertain deliveries only after inspection and an explicit
decision; unknown native state is not proof that replay is safe.
