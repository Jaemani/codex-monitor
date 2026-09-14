# Unattended CLI conversations

Use this workflow when the user requests keeping selected CLI conversations available or restoring
their owner connection. Inspect `resident --help` to verify the installed runtime supports it.

Use one explicit owner endpoint shared by the ordinary `codex --remote` TUI, the receiver binding
and the resident. Use exact IDs of authorized CLI monitoring tasks. A default Desktop shared-local
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

Keep ordinary shared-local setup only for CLI-owned local conversations. Desktop requests
use a new, separate CLI monitoring task under the SKILL.md execution-target gate.
Do not migrate an existing Desktop task or replay an incident merely because it is queued.


## Requests made in Desktop

Refuse the requesting Desktop conversation as a monitoring target. For new setup, create a new,
separate CLI monitoring task only when authorized; SSH is not required. A new Desktop chat or monitor
definition is insufficient: the target must have its own distinct thread ID and a verified CLI owner
and resident. Keep the requesting Desktop task for discussion.

Do not attempt same-task migration, wait for Desktop ownership release, or ask for whole-app shutdown.
An ownership conflict is a blocker for that target, not a transfer workflow. Existing explicitly
selected CLI monitors can still be inspected and managed after verifying their owner. Do not promise
simultaneous Desktop/CLI ownership or automatic replies back to Desktop; a relay requires a separate,
explicitly authorized configuration.

After setup, tell the user the project/group, exact conversation, endpoint, watched condition, and
which producer, receiver and owner/resident observations passed. Give concrete commands to open
`connect --endpoint ENDPOINT --thread THREAD_ID --cwd /absolute/project`, view `dashboard`, and inspect
`monitor status NAME --thread THREAD_ID` or `sessions BINDING`. Explain that dashboard dots do not prove
end-to-end consumption. Provide the route controls: Tab selects a route; `p` stops, `r` resumes, and
`x` then `y` removes it. These actions preserve native history, do not retract accepted input, and do
not restart external producers. Approval requests must be handled in the same-owner native TUI.
For unattended use, report supervision for the owner and resident as well as the receiver; an open
foreground tool process is not a durable installation. Keep unchanged status checks outside chat.
