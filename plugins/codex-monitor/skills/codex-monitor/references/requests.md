# Explicit work requests

Use this flow when the user's task requires tracking external work, rather than
only observing an event. An original external delivery receipt is required.
Managed file events have no external issuer and cannot be tracked this way.

Run these arguments through the bundled helper with the same state:

```text
request track DELIVERY_ID --key REQUEST_KEY --thread THREAD_ID --summary "Investigate the build" --expires-in 3600
request list --thread THREAD_ID
request status REQUEST_ID --thread THREAD_ID
request update REQUEST_ID --thread THREAD_ID --state in_progress --update-id started-1 --revision 0 --message "Investigation started"
```

Read the returned revision before updating. Stable update IDs deduplicate
identical retries; changed content or stale revisions conflict. Preserve the
original source and conversation. Paginate lists with the returned `next`
cursor as `--after`.

States are received, acknowledged, in_progress, completed, failed, cancelled and
expired. Tracking and acknowledgement stay quiet. Progress and terminal changes
create ordered notifications for the original conversation. Record completed
only when the authorized work's evidence supports it. Native consumption,
assistant text and reply acknowledgement do not prove completion. Expiry and
cancellation record tracking state; they do not terminate an external process.

Inspect notification delivery IDs separately with `inspect`. A paused binding
holds notifications, and an absent receiver leaves them pending. Reply to the
original external source only when the user's task authorizes it, using the
explicit reply workflow in SKILL.md. Request state updates do not implicitly
send a message to a remote person.
