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
attach subscriptions. Existing queued events may begin processing on registration. Without explicit permission options it changes no
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

## Permission preflight and user choice

A successful Discord reply verifies only the reply path. It does not establish permission to create
worktrees, child conversations, bindings or services. Before provisioning these resources, including Discord thread/channel creation, perform this mandatory preflight:

1. Map the requested operations to the effective runtime policy, required write locations and
   service/API permissions. Shared Git metadata, worktree roots, adapter configuration, monitor
   state and service definitions may all be outside the project workspace. Inspect the exact paths;
   do not claim the whole home directory is required. Host file modes or an unsandboxed access
   check alone do not establish access from the target Codex runtime.
2. Prepare the concrete setup plan and explain which operations are currently allowed or blocked.
   Distinguish sandbox denial, operating-system permissions and Discord API permissions. List only
   access actually needed. Existing authorization counts; do not repeatedly ask for the same scope.
3. If expanding permissions is necessary and not yet authorized, ask the user to choose:
   - Targeted access to the identified paths and required operations, retaining other restrictions.
   - Temporary full access for this setup, followed by restoration of the prior policy.
   - Keep current restrictions and leave the dependent setup pending.
   Explain that full access removes filesystem and network isolation. Targeted write access may not
   cover service management or protected paths; verify native support rather than promising it.
4. Apply only the authorized choice. Check active turns, queued work and other conversations sharing
   the owner before a policy change or restart. Save the prior policy if temporary access was chosen.
   Do not create Discord threads or other dependent external resources until the required local
   configuration is feasible, unless the user explicitly requests partial setup.
5. Verify effective permissions in the actual target runtime. After provisioning, restore temporary
   access and verify the restored state before declaring setup complete. Report readiness for normal
   monitoring separately from readiness for later administrative changes.

For ordinary reply commands, supported resident versions can use
`resident --endpoint ENDPOINT --thread THREAD_ID --sandbox workspace-write --network-access`.
This permits outbound networking but does not grant writes outside the workspace. Persist matching
owner configuration for clients that resume first; verify installed options before use. Do not
silently select full access after a filesystem error or present it as the only possible solution.

User-facing explanation example: "Replies can reach Discord, but creating these worktrees and
updating these routing files needs additional write access: [verified paths]. Choose targeted access,
temporary full access for setup with restoration, or leave setup pending."

The dashboard provides a separate Change Permission menu with Full Access, Read-only and Project Access. Ordinary Reconnect preserves permissions.
Each requires a preview and the same action again within 60 seconds. These actions change the
persistent defaults for all conversations sharing the verified owner, not just the selected row.
Full access is not automatically temporary; after temporary setup authorization, explicitly restore
the approved restricted mode and verify it. Read-only also disables command networking, so local
Discord reply commands will fail; an external reply adapter has separate permissions. Do not click
a permission action on the user's behalf unless the chosen scope is authorized.
