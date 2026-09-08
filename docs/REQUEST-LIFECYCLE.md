# Requests in an existing conversation

The CLI and source-authenticated HTTP workflow is implemented. Installed
macOS/Linux checks and actual CLI TUI evidence are recorded in STATUS.md.

A tracked request links one original external event receipt to one existing conversation. Its issuer source, original binding and conversation do not change. The same request key in two conversations or sources identifies independent requests.

## State and delivery are separate

The request state records an explicit report: received, acknowledged, in progress, completed, failed, cancelled or expired. It does not infer success from native queue consumption, an assistant response, or a reply acknowledgement. A report of completion must still be assessed against the work's system of record.

Meaningful state transitions create durable notification outbox entries. The receiver places those notifications into the original conversation using stable event IDs. Request state, local notification intake and native consumption remain separate fields. Replaying after an intake/receipt crash uses the same event ID.

Initial tracking and acknowledgement are quiet. Progress and terminal transitions can notify the owning conversation. Explicit replies still use the original source-scoped reply outbox; ordinary assistant responses are never forwarded automatically.

## CLI workflow

Use the same state directory and the exact current conversation throughout:

```bash
codex-monitor request track "$DELIVERY_ID" --key build-17 \
  --thread "$THREAD_ID" --summary 'Investigate build 17' --expires-in 3600
codex-monitor request list --thread "$THREAD_ID"
codex-monitor request status "$REQUEST_ID" --thread "$THREAD_ID"
codex-monitor request update "$REQUEST_ID" --thread "$THREAD_ID" \
  --state in_progress --update-id started-1 --revision 0 \
  --message 'Investigation started'
```

Lists are bounded to 100 records by default. Use `--limit` and the returned
`next` cursor as `--after` to read further pages in the same conversation.

Read the current revision before a transition. Reusing an update ID with the same content is an idempotent retry; a changed body conflicts. Stale revisions and illegal regressions are rejected rather than silently overwriting newer work. Update IDs are local to their request.

The existing exact-thread rules apply: host-provided CODEX_THREAD_ID and an explicit --thread must agree. A CLI status query does not wake the model. Terminal transitions cannot be undone by a delayed progress report.

When a receiver is running, `track` and `update` require it to advertise request
lifecycle support before changing stored requests. A missing capability or
failed probe produces a restart error. Offline mutations remain local and are
allowed; notification dispatch waits for a compatible receiver to start.

## Source integration

Source-authenticated HTTP operations use the source attached to the original receipt. They cannot retarget another source's request or select a different conversation through event text. Administrator credentials are not treated as source credentials.

Source responses expose per-request update capacity only. Store-wide activity
counts remain available to the local operator and are not included in another
source's HTTP response.

| Method | Route | Body or query |
|---|---|---|
| POST | `/v1/requests` | `delivery_id`, `request_key`, optional object `payload` and Unix timestamp `expires_at` |
| GET | `/v1/requests` | Required `thread`; optional `limit` and `after` cursor |
| GET | `/v1/requests/REQUEST_ID` | No query |
| POST | `/v1/requests/REQUEST_ID/updates` | `update_id`, `state`, `expected_revision`, optional `detail` |

Use the same source bearer credential as event intake. JSON request bodies are
limited to 32 KiB, stored request payloads to 16 KiB and transition details to
4 KiB. Browser origins and chunked bodies are rejected. Managed file events
have no external issuer and cannot serve as original request receipts.

Tracked requests are records of authorized work, not permission grants. A request payload cannot enable tools, approve actions, select a new recipient or override the user's task. The producer is responsible for the truth of its status reports.

## Recovery and limits

Pausing the original binding holds notification delivery. It does not undo a recorded transition or retract a native accepted event. Reopening a client may allow queued notifications to be consumed, but a receiver cannot force an inactive native conversation to execute work.

The receiver persists expiry transitions without asking the model to poll. An expiry marks a tracking deadline; it does not terminate an external worker. Cancellation likewise records intent and can notify the conversation, but does not prove an external process stopped.

See [reliability limits](RELIABILITY-LIMITS.md) for native-client, filesystem and kernel constraints. Completed records and idempotency evidence require an explicit retention policy; they are not silently deleted.

The default store retains at most 10,000 requests and 10,000 pending
notifications. It reserves terminal notification capacity for open requests.
Capacity exhaustion rejects new work explicitly. Automatic retention and a
safe archival interface remain future work; do not delete databases to make
space while deliveries or requests are unresolved.
