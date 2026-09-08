# Event source adapters

Commands using `.venv/bin` assume the development environment described in the [README](../README.md#development). For a runtime installation, use the executable path printed by the installer.

External input uses one authenticated JSON event envelope. Receiver-owned managed collectors use an internal reserved source that external requests cannot impersonate. A source adapter interprets ALM, GitHub, CI, file, or agent-specific input. The monitor does not duplicate project role rules or source transport implementations.

## Event contract

Required fields are `id`, `source`, `type`, and `data`; `trace_id` and `hops` are optional. Each ID, source, type, and trace identifier is limited to 200 characters. `data` must be finite JSON, and the complete event must not exceed 32 KiB. Both the binding's allowed sources and the authenticated token's source must match.

Preserve a stable event ID from the originating system. Retries must reuse that ID.

| Result | Sender action |
|---|---|
| `202` | The durable local inbox accepted the event. Codex processing may still be pending. |
| `429`, `503`, or connection failure | Retry with the same event ID. |
| `400`, `401`, `403`, or `409` | Correct the request or configuration before retrying. |

Never put a webhook secret in a URL query. If an external service cannot send the required headers or JSON, its adapter must verify the service's signature and convert the request to the common envelope. This implementation does not claim to verify native GitHub webhook signatures. Operators configure reverse proxy/TLS and source secrets for external connections. The server binds only to loopback. Clients do not follow redirects that could disclose a source token to another destination.

## Agent-to-agent messages

Give agent A and agent B separate source credentials. A's conversation binding allows only source B; B's allows only source A. Each sender receives only its own token.

```bash
.venv/bin/codex-monitor send --to agent-b-chat --source agent-a   --id task-17-request --trace task-17 --hops 0 --type agent.message   --data '{"task":"review","ref":"example-ref"}'
```

A reply keeps the trace, increments `hops` by one, and uses a new event ID. A sender must not forge its source or use the other sender's token. An `agent.ack` is recorded without waking the model. The monitor does not generate automatic acknowledgements or forward every model response to another agent.

Limits are 8 hops, 16 events per trace by default, and 120 events per binding per minute. Creating unlimited new traces would bypass a per-trace limit, so projects must also enforce their own work and cost budgets.

## Managed file watching

Prefer `codex-monitor monitor create NAME --thread "$THREAD_ID" --file /absolute/path` for a receiver-owned, conversation-scoped collector. No source registration is needed; `serve` restores its checkpoint after restart. See [conversation monitors](CONVERSATION-MONITORS.md).

## Legacy foreground file watching

```bash
.venv/bin/codex-monitor source health
# Restart serve after adding the source, and allow health on the target binding.
.venv/bin/codex-monitor watch-file /path/to/status.json --to health-chat --source health
```

The first sample establishes a baseline without sending an event. Later samples emit `monitor.changed` only when the content hash changes or the file's existence or readability changes. Events do not include the file contents. Sampling occurs outside the model every two seconds by default. Prefer a direct webhook when the source supports push delivery.

The watcher stores the pending event and ID before sending. After a lost response or watcher restart, it retries the same event. An OS lock rejects another watcher for the same file, target, source, and URL. Multiple changes during a failure may collapse to the latest observed state, so this is not an audit log of every file write.

## Extending the monitor

Pass JSON samples to `ChangeWatcher(path, source, event_type, emit).check(sample)` to reuse durable change detection. The `emit` callback posts the common HTTP envelope. Adapters collect network or process state. There is no interface that executes a remote event payload as a shell command.

A destination adapter implements `deliver(thread, client_id, text)` and `reconcile(thread, client_id)`. It must report an uncertain delivery failure as `Uncertain`. Automatically replaying a message that may already have been delivered violates the adapter contract.
