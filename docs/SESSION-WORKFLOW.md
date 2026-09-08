# Reuse one conversation for monitoring

A session names an existing conversation selected by the user. It does not
create a new agent conversation or intercept the user's input. When the
receiver is running and the session is enabled, an allowed source can deliver
events to that conversation. Session state is checked through the CLI/API;
the normal Codex UI does not show a separate background-job indicator.

For a local file, prefer [conversation-scoped managed monitors](CONVERSATION-MONITORS.md): the receiver owns sampling and checkpoint recovery, and each conversation has its own names and controls. The source/attach workflow below is for external producers.

## Attach and inspect

Run these commands from an initialized state directory. `THREAD_ID` is the
conversation that should continue to receive events:

The examples assume the installed `codex-monitor` launcher is on `PATH`. The
installer does not edit `PATH`; if it prints an absolute launcher path, use
that path instead.

```bash
codex-monitor source build
codex-monitor attach work --thread "$THREAD_ID" --source build
codex-monitor serve
```

In another terminal:

```bash
codex-monitor sessions
codex-monitor sessions work --json
codex-monitor pause work
codex-monitor unpause work
```

These queries do not call Codex or the model. `Receiver: running` means the
receiver process exists; `enabled` means that session allows intake and
delivery. Neither proves that an external collector is healthy or that the
model is working.

While paused, new events are rejected and undelivered monitor input is kept.
Input already accepted by Codex is not deleted. If the user presses Ctrl-C to
stop a Codex turn, `unpause` does not undo that cancellation; the user can send
the next message in the same conversation.

## Send selected events

External collectors should suppress unchanged periodic state. The built-in
`watch-file` does not create model input for the initial state or an unchanged
state, and webhooks deduplicate stable event IDs.

```bash
codex-monitor send --to work --source build --id build-17 \
  --type build.failed --data '{"message":"Build 17 failed during the authentication test.","job":17}'
```

The conversation shows a readable event summary, source, and short receipt
reference rather than the raw envelope. Full details remain available through
the receipt and `event DELIVERY_ID`; long data is truncated for display. With
the current native-queue path, the event input remains in the conversation.

## Explicit replies

Users continue to work in the same conversation. A response for an external
source is selected explicitly and may be sent by the user or by an agent using
the user's existing authority:

```bash
codex-monitor reply "$DELIVERY_ID" --id decision-17 \
  --message 'Update the authentication settings and run the build again.'
```

Replies belong only to the originating source and are stored in a durable
outbox. Normal conversation answers are not copied automatically. Retries use
the same reply ID and body; a different body for an existing ID is rejected.
The source retrieves the reply rather than having the monitor send it to an
arbitrary URL or messenger.

The source uses its existing bearer token:

| Request | Behavior |
|---|---|
| `GET /v1/replies` | Returns up to 100 unacknowledged replies for that source. |
| `POST /v1/replies/REPLY_ID/ack` | Empty body; acknowledges one reply and is safe to repeat. |
| `GET /v1/sessions` | Reads receiver/session status with the admin token only. |

A source cannot read or acknowledge another source's replies. Acknowledge a
processed batch before reading the next one. Acknowledgement does not wake
Codex. After a network failure, the same reply may be returned again, so the
source should deduplicate by reply ID. The default outbox limit is 1,000
replies and each reply body is limited to 16 KiB.

## Confirm delivery and recover

`sessions` reports session and local event state. For native placement, use the
read-only command:

```bash
codex-monitor inspect "$DELIVERY_ID"
```

It distinguishes local `accepted` from native `queued`, `consumed`, and
`unknown`. `accepted` and `consumed` confirm placement, not task success. See
[OPERATIONS.md](OPERATIONS.md) for retry and reconciliation rules and
[CANCELLATION-QUEUE.md](CANCELLATION-QUEUE.md) for interrupted-queue behavior.

If you use `--state`, pass the same state path to every CLI command. Configure
the session name, state path, and permitted work before giving an agent access.
An event description does not grant new permissions.
