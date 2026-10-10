# Live connection dashboard

The canonical dashboard is native Rust. `Project permissions` / `Server permissions` is a shared-server
action: its separate menu identifies the server and its preview lists every
affected conversation, including other projects. Project groups are navigation
labels, not permission isolation boundaries. Reconnect retains permissions.
The receiver resource totals exclude Codex owners and their tools; reusable sampler
workers are shared and are not billed as project-owned agent processes.

## Reconnect with the current login

The dashboard's **Reconnect** action replaces the earlier **Retry auth**
button. Refreshing a cached token does not switch a long-lived owner from an old
account to the current saved login.

Select a connection and choose Reconnect. The first action inspects the matching
local macOS user LaunchAgent and lists all loaded conversations sharing it.
Select Reconnect again within 60 seconds to confirm. The action verifies the
current saved ChatGPT login in a short-lived, unsubscribed App Server, verifies
that every saved conversation exists, rechecks the owner configuration and idle
states, then restarts that exact registered owner. Existing resident services
reattach the same conversation IDs. Completion requires a changed owner PID,
matching account information, authenticated account access and restoration of all
previously loaded conversations. Failed input is not replayed; use Open in Codex
to continue it. Queued input may run when residents reconnect.

Existing Codex windows disconnect during the shared restart. Active or unverified
conversations, missing or unverifiable saved model providers, missing resident coverage, different Codex homes, changed service
configuration and expired previews block the action. Idle-state checks are a
preflight observation, not an atomic guarantee against new input arriving during
a restart. A cross-dashboard lock prevents simultaneous reconnects of the same
endpoint, and each dashboard limits restart attempts to one per 30 seconds.
Verification failure after restart is reported without another automatic restart.
The background worker retains responsibility for its temporary child even if the
UI closes during recovery.

This implementation supports direct Codex user LaunchAgents with exact explicit
local endpoints and running codex-monitor resident LaunchAgents. It does not
infer arbitrary process ownership, modify plists, copy tokens, change account
configuration, restart remote hosts, or manage Desktop's internal server.
Unsupported setups must reconnect using their own process supervisor.

No named custom provider is required. The built-in OpenAI provider needs no custom
provider entry. For another saved provider, preflight reads effective configuration
for that conversation's project directory and requires its provider definition to
exist. This preserves optional custom providers without copying configuration,
forcing a provider name or silently replacing the conversation's provider. The
check establishes configuration presence, not successful custom-provider model
execution or credentials.

For explicit owners reporting systemError, details show the newest failed turn's
native error message near the top, with common credential forms redacted. Only one
turn without items is read; older failures do not replace the current error.
Unsupported history reads are stated explicitly. Dashboard refreshes remain
read-only and never invoke Reconnect. JSON snapshots include execution_error.

Protocol reference: [App Server authentication and thread history](https://learn.chatgpt.com/docs/app-server).

## Resource and delivery observations

The native dashboard shows receiver-tree RSS, CPU and process count in its shared
header. It excludes Codex owners, their tools and other dashboards. Sampling workers
are reusable and shared across projects; project ownership is not inferred from
those workers. Unknown attribution stays unknown. RSS samples can miss short-lived
processes and do not represent macOS compressed physical memory.

Process and storage observations are cached for 30 seconds. Storage scans skip
symlinks and have a time/entry budget. Event ages describe unresolved delivery,
not model completion. External producer health requires its own observation.

The dashboard is a separate terminal view of one local codex-monitor state directory. It reads
configured conversations, managed collector observations, delivery receipts and explicit request
reports. Refreshes use bounded read-only probes for explicit Codex owners; they do not create model turns or change monitoring
configuration. Explicit actions in the detail overlay can pause, resume or remove the selected route,
or open its ordinary Codex TUI. Enter on the overview opens details.

```bash
codex-monitor dashboard
codex-monitor dashboard --thread "$THREAD_ID"
codex-monitor dashboard --once
codex-monitor dashboard --once --json
codex-monitor dashboard --no-animate
codex-monitor dashboard --color never
```

Use `--state /absolute/state` before `dashboard` for a different local receiver. The default refresh
interval is two seconds; `--interval SECONDS` changes it. Live mode needs an interactive terminal.
Use snapshot mode in scripts and redirected output.


The dashboard observes **event delivery**, managed collector observations and explicit request work
reports. It does not collect live model/tool execution, token usage, cost or inferred completion
percentages. `Last activity` shows the latest stored event observation, not completed agent work.
Connection counts refer to registered routes, not live sockets or running agents.
JSON snapshots include a `scope` object describing this boundary, including on read failures. A scope
field describes what the view can report, not whether a producer or consumer is currently healthy.

## Controls

- Arrow keys: move spatially between conversations in project panels.
- Enter or click a conversation: open its detail overlay.
- Esc: close details; from the overview, exit the dashboard. `q` and Ctrl-C also exit.
- Up/Down or mouse wheel in details: scroll long content.
- Tab in details: inspect the next registered connection, including paused entries.
- Left/Right in details: select a visible action; Enter activates it. Actions are also clickable.
- Open in Codex: launch the saved conversation on its explicit owner. Exit that TUI to return.
- Reconnect: preview a shared owner restart with the current login, then select again to confirm.
- Pause/Resume: change monitoring for the displayed connection.
- Remove: ask for confirmation. Enter confirms; Esc or another key cancels. The confirmation
  retains its exact original target even if the inventory changes.
- Resize: project panels reflow into one, two or three columns. Selection stays visible.

The dashboard restores terminal settings and disables mouse reporting on exit or TUI handoff.
Refreshes remain read-only. Route controls apply to the connection displayed in the overlay. Reconnect is owner-wide
and previews all affected conversations before confirmation. Use Tab to inspect the intended route first.
Do not infer authentication or completed work from an enabled route.
For managed file monitors, stop/resume also updates the collector lifecycle. For external sources,
resume re-enables intake and delivery; it does not restart a separate producer process or load an
unloaded native conversation. Stopping or removing monitoring does not terminate a Codex turn, archive the conversation or erase
its history. Delivery already in flight or submitted to Codex may still finish. The dashboard does not replay
events, send replies or restart the receiver.
Removal retires the route and preserves receipts; it is not a reversible pause. Retired external
binding names remain reserved, so use a new name when registering a replacement. Recreating a
managed file monitor creates a new generation.

## Project groups and stable names

Projects and display names are explicit per-conversation metadata, independent of routing IDs.
New conversations appear under **Ungrouped** until assigned. A project group is an organizational
label; it does not create a parent agent or broadcast events. Different projects may use the same
name, such as PM or Mobile. Duplicate names within one project receive an ID suffix in the view.

```bash
codex-monitor conversation set --thread "$THREAD_ID" --project "My project" --name "Mobile"
codex-monitor conversation list
```

All routes for that exact conversation inherit its group and display name. Adding another route
will not rename a conversation with an explicit display name. Without one, the dashboard derives
a fallback from saved route names; that fallback is not the native Codex conversation title.
The assignment is local to the selected monitor state directory and does not modify Codex projects.

## Open a conversation and interact

Run `codex-monitor dashboard`, select a conversation and press Enter for details. Use the
Open in Codex action to launch it. For multiple connections, use Tab to choose the intended route.
Selecting a conversation initially prefers an enabled explicit owner route. The dashboard
temporarily hands the terminal to `codex --remote ENDPOINT resume THREAD_ID`. In that TUI you can
read the conversation, watch new event responses, send messages and answer native approvals.
The existing conversation ID and saved endpoint are retained; no replacement task is created.
Opening a task subscribes to it and may allow already queued input to be processed.

Opening requires a stored WebSocket (`ws://` or `wss://`) or absolute Unix socket endpoint on the
binding. Plain `ws://` must use loopback; use `wss://` for a non-local owner. Existing
`CODEX_MONITOR_SERVER_TOKEN` or `CODEX_MONITOR_SERVER_TOKEN_FILE` authentication is passed to
Codex through the environment, never as a token value on the command line.
A `shared-local` binding identifies shared storage rather than its owning server, so the
dashboard cannot safely infer where to attach. Configure the correct explicit owner endpoint first;
the dashboard will not silently fall back to an independent `codex resume` or transfer ownership.
The owner must already be running. Keep the resident running if you need processing after closing
the TUI. See [CLI owner setup](OWNER-LIFECYCLE.md).

Use separate terminal tabs to open different conversations side by side. This view lists monitor
bindings in the selected state directory; it does not discover every native agent or automatically
configure messages between agents. Dashboard refreshes stay quiet; interacting with Codex uses
the selected conversation's normal model and permissions.

## Reading the screen

The top summary separates receiver readiness, observed execution failures, pending events on
enabled routes, and conversations whose delivery readiness has not been verified. These are
observations, not live generation telemetry. The attention panel shows up to four actionable
conversation issues; all conversation states remain available in the project panels.

Project panels use saved names and neutral borders. Cyan identifies selection and delivery
readiness; red indicates failures, amber indicates review or unverified state, and gray indicates
paused routes. Every state also has a text label, so color is not required.
Taller terminals add last activity beneath each conversation. Shorter terminals retain compact
rows and automatically scroll to the selected conversation. Wide terminals show inventory age
in the footer when space permits. None of these ages establishes completed work.

Details appear in a centered, bounded overlay over the dimmed overview. Delivery observations,
explicit execution reports, recorded errors and source paths have separate labeled sections.
Scrolling details does not move the selected conversation. Closing restores the same overview.
Diagnostic `--once` and JSON output retain the detailed inventory independently of this layout.
Use `--color auto|always|never`; auto honors `NO_COLOR` and `TERM=dumb`.

| Field | Meaning |
|---|---|
| Receiver process | Whether the receiver's local process lock is held |
| Authenticated status | Result of a bounded loopback `/v1/status` check; HTTP errors and worker errors are distinct from process liveness |
| Conversation and bindings | Stored routing configuration in this state directory; enabled or paused does not establish client presence |
| Collector observations | Persisted local sampler status, observation age and errors; old observations must not be treated as current activity |
| Events | Stored states such as pending, submitting, uncertain, accepted or dead; counts are not counts of completed tasks |
| Latest receipt | Identity and age of the latest stored delivery; use `inspect DELIVERY_ID` for explicit native queue/history evidence |
| Requests | Explicitly reported request states and latest report; not automatically inferred agent execution state |
| External producer health | Unknown until a supported producer observation exists; successful old events are not a live connection check |

The screen shows registered connections across conversations, not every native Codex agent. It does
not infer model selection, token use, current generation or successful work from event traffic. A
thread filter selects the inventory to display; it does not retarget any delivery.

## Relationships

The dashboard groups routes by their destination conversation. A managed monitor belongs to one
conversation; several monitors can feed that conversation. Several conversations can share one
receiver without sharing a parent agent. A binding routes permitted sources to one exact conversation;
allowing a source on multiple bindings does not automatically broadcast its events.

A PM or relay conversation is optional and must be selected explicitly. The dashboard does not infer
native agent teams, ancestry or work dependencies. See the [domain vocabulary](../CONTEXT.md).

## Failures and scope

Missing, unreadable or incompatible databases appear as inventory errors. Live mode can retry after
the state becomes available. A stopped receiver does not erase its saved connections. Native target
checks are intentionally outside the refresh loop, keeping dashboard reads independent of model work.
An explicit open checks that the selected binding still matches the displayed identity and endpoint;
changed or unsafe identities are rejected. Connection failures appear in the Codex TUI, and its exit
status appears when the dashboard returns. Opening is not proof that a task completed its work.

Snapshot SQLite connections use read-only mode without runtime constructors or migrations.
Explicit monitor controls recheck the displayed binding, conversation and endpoint in the write
transaction; stale targets are rejected. Local reads
and status probes are bounded; the HTTP check stays on configured loopback, does not use environment
proxies, and does not follow redirects. Token contents and event bodies are not displayed.

This dashboard is not a cross-host fleet manager or a native Codex badge. Start a view for each state
you operate. External producer telemetry and a richer native agent-activity integration are separate
future features. OS-timed probes can already feed failure events using the [health-hook example](HEALTH-HOOK.md).

Validation is recorded in [TESTING.md](TESTING.md). A dashboard PTY test verifies this terminal view,
not a Codex CLI conversation or Desktop event-delivery flow.

## Inspecting connections

A conversation can receive events through many connections. Tab in the overlay cycles through
all registered connections, including paused entries. The connection name, enabled state and
position are shown together above delivery and execution facts. Up/Down scroll long content.
Queue acceptance does not establish native consumption or task completion. Opening requires
an explicit owner endpoint; queue-only routes cannot infer an owner.

## Reconnect with a permission mode

Conversation details keep **Reconnect** (preserve permissions) separate from
**Project permissions** (one project) or **Server permissions** (mixed or unknown
projects). Open this menu to see the saved policy, inherited owner defaults and
choose **Full Access**, **Read-only**, or **Project Access**. Use Left/Right
and Enter; Back or Esc returns to the details without applying a choice.
The first activation of a permission mode previews
all configured conversations affected by the shared owner and the persistent
policy change. Activate the same action again within 60 seconds to confirm.
Changing actions discards the previous confirmation. Ordinary Reconnect retains
service configuration.

| Mode | Filesystem | Command network |
| --- | --- | --- |
| Full | Unrestricted by the Codex sandbox | Enabled |
| Read-only | Writes disabled | Disabled |
| Workspace+Net | Project workspace writes; external paths restricted | Enabled |

These are shared-owner service settings, not per-conversation overrides. The
preview lists the configured conversation IDs, including currently unloaded ones.
Active work, nonempty queues or an unverified local service block the operation.
Saved owner/resident definitions are backed up before modification. The owner and
residents are reloaded, and permissions on previously loaded conversations are
verified. Failed updates attempt to restore the previous definitions; any partial
restoration is reported. No failed prompt is replayed and no synthetic model turn
is started. This currently requires verified macOS user LaunchAgents.

The choice persists until changed; Full does not expire automatically. If full
access was authorized only for setup, select the prior restricted mode after the
work and verify it. Read-only can prevent both configuration work and Discord
reply commands. Workspace+Net does not enable writes to external configuration,
worktree or service paths. Discord thread/channel creation requires the permission
preflight and user choice described in OWNER-LIFECYCLE.md; full access itself is
not mandatory and does not grant missing Discord bot permissions.

The overview and details show saved permission labels in English: `Read-only`,
`Full Access`, `Project Access`, `Workspace`, `Check permissions` or `Access unknown`.
These observations come from saved service settings, not verified live permissions;
this distinction is explained in details rather than repeated on each overview row:
they read matching resident service definitions without resuming a conversation.
The completed permission-change operation separately verifies the effective mode.

All detail actions remain visible: the footer wraps buttons into rows instead of
paging them. Project Access means editing files in the project workspace and
using the internet; writes outside that workspace remain restricted. This label
renames the existing workspace-write plus network policy without changing access.

When active connections exist, permission summaries exclude disabled legacy
connections. Conflicting or partially unknown active connections require inspection
and display Check permissions. All action buttons occupy one horizontal row when
they fit; wrapping occurs only when the terminal is too narrow. The detail dialog
uses up to 112 columns, with the permission menu and Close always present.

## Back to codex-monitor Dashboard

Opening Codex from a connection temporarily hands the terminal to the native Codex
TUI. The dashboard displays **Back to codex-monitor Dashboard: use /quit in Codex**
before handoff and the same return label afterwards. `/quit` is a built-in Codex
command; codex-monitor does not rename its native command-picker description.
For an explicit remote owner this detaches the TUI and returns to the waiting
dashboard. The shared owner, residents and receiver continue running. Do not stop
the owner process to return to the dashboard.

## Installed dashboard updates

Native installs use immutable executables. After installing an update, exit the
old dashboard with `q` and run `codex-monitor dashboard` again. Existing owner and
resident conversations do not need to be restarted for display changes. Updated
dashboards detect subsequent native installs and display a reopen banner.
Approval and response waits appear in the attention summary and panel; they are
not counted as Ready. Unsupported or missing decision state is shown as Needs
review. SSH alerts still require a live dashboard and terminal
support for the selected OSC 9 or BEL backend.
