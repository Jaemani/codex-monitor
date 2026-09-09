# Unattended CLI conversations

Use this workflow when the user requests keeping selected CLI conversations available or restoring
their owner connection. Inspect `resident --help` to verify the installed runtime supports it.

Use one explicit owner endpoint shared by the ordinary `codex --remote` TUI, the receiver binding
and the resident. Use only user-selected existing conversation IDs. A default Desktop shared-local
writer is not that owner; there is no verified unattended Desktop equivalent.

The foreground lifecycle command is:

```text
resident --endpoint ENDPOINT --thread THREAD_ID
```

Repeat `--thread` for additional explicit conversations. This deliberately calls `thread/resume` to
attach subscriptions. Existing queued events may begin processing on registration. It changes no
permission policy and submits no synthetic prompt. Keep the process under the user's authorized
OS supervisor for unattended use; do not occupy the assistant's tool loop indefinitely.

The owner App Server and receiver are separate processes. The built-in `service` command supervises
only the receiver. Resident reconnects to the same endpoint and restores subscriptions when the owner
returns, but does not restart the owner process. Retain owner storage and thread arguments across
process restarts. Report connected/subscribed/ready separately from task completion and human input.

Return interactively with `connect --endpoint ENDPOINT --thread THREAD_ID --cwd /absolute/project`.
Approvals and questions belong to that native TUI. Resident never answers them. An active-writer
conflict requires using the existing owner; preserve queued receipts instead of competing or replaying.

Keep ordinary shared-local setup for users who only want events in a loaded local conversation.
Do not migrate an existing Desktop task or replay an incident merely because it is queued.
