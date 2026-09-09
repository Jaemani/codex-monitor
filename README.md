# codex-monitor

**Keep working in your Codex conversation. Let external events come to you.**

codex-monitor connects file changes, webhooks and worker events to an existing **Codex CLI or Desktop conversation**. A small receiver waits outside the model, stores events and delivers them through Codex's official queue API. You can keep typing in the same conversation.

[Quick start](#quick-start) · [Execution model](#what-runs-where) · [Use cases](#what-can-i-use-it-for) · [Comparison](#how-it-compares) · [Status & evidence](docs/STATUS.md) · [Roadmap](docs/TODO.md)

## Why use it?

- **Keep the context.** Human messages and external events reach the conversation you chose.
- **Stay quiet between changes.** Waiting, file sampling and dashboard refreshes require no model turns. Event processing still uses the selected model.
- **Keep delivery inspectable.** Stored receipts, stable event IDs and explicit reply tracking help diagnose missing or uncertain deliveries.
- **Separate conversations when useful.** Route several sources into one PM conversation, or use independent conversations for different projects. A relay agent is optional.
- **See the connections.** A separate terminal dashboard shows configured conversations, collector observations and delivery states without adding chat messages.

This is an event-delivery and monitoring layer. It does not replace Codex's agent orchestration, permissions or task UI. External messaging services need their own adapters.

## What runs where?

```mermaid
flowchart LR
    E[External event or file change] --> P[Producer: receives or detects changes]
    P --> R[codex-monitor receiver: authenticates and stores]
    R --> Q[Official Codex conversation queue]
    Q --> C[Existing CLI or Desktop conversation]
    U[You] <--> C
    D[Terminal dashboard] -. observes local state and receiver .-> R
    D -->|Enter: open selected CLI conversation| T[Ordinary Codex TUI on saved owner]
    T <--> C
```

| Component | What it does | When it runs |
|---|---|---|
| Skill: `$codex-monitor` | Helps configure, inspect and manage monitoring | When an assistant uses it; installing it alone starts nothing |
| Producer | Receives a webhook/Gateway event or detects a file change | Outside the model; managed files are supervised by the receiver |
| Receiver | Authenticates, persists, deduplicates and routes events | One long-lived local process; can serve multiple conversations |
| CLI resident (explicit owner mode) | Retains selected conversation subscriptions and restores them after connection loss | Separate foreground process alongside the shared App Server; no model polling |
| Codex conversation | Reads events and performs the authorized work alongside user input | Native Codex processing; busy conversations queue events |
| Dashboard | Reads monitor state and checks receiver readiness; Enter opens the selected CLI conversation | Refreshes without model calls; explicit TUI interaction follows normal Codex behavior |

**You normally run one receiver, your usual Codex client, and optionally the dashboard.** A Discord integration also needs a Gateway producer and reply adapter. It does not need an always-running model or a separate relay conversation.

The default `shared-local` path uses the same OS user and Codex store (`CODEX_HOME` / `sqlite_home`) as the target client. An independent App Server writer adds input through the official queue API; it does not type into the UI or start/resume conversations. Local Desktop does not require SSH.

In the validated Codex 0.153.4 path, the native consumer checks external changes roughly every 10 seconds. Busy turns can add delay. If the target conversation is unloaded (even while Desktop stays open), input remains queued until an owning client loads it. Keeping the receiver running does not keep every registered conversation loaded. **Event-driven does not mean an immediate model response.** See [latency measurements and direct-owner options](docs/LATENCY.md) and [queue/compatibility troubleshooting](docs/TROUBLESHOOTING.md).

**CLI is the current development priority for unattended conversations.** The explicit-owner
`resident` command keeps selected CLI conversations subscribed and restores those subscriptions
after connection loss. The ordinary TUI connects to that same server. This requires an owner and
resident process in addition to the receiver; see [setup and reconnect behavior](docs/OWNER-LIFECYCLE.md).
Default local Desktop does not yet have a verified owner connection for this lifecycle. A running
Desktop app alone therefore cannot guarantee background processing of every saved conversation.

## Quick start

Requires **Python 3.11+**, Codex CLI and a local CLI/Desktop conversation. The installer supports macOS and Linux and installs the Python `websockets` dependency.

```bash
git clone https://github.com/Jaemani/codex-monitor.git
cd codex-monitor
python3 scripts/install.py --with-skill install
codex-monitor --help
```

The default installer registers `~/.local/bin/codex-monitor`. If that directory is not on your
`PATH`, follow the printed shell hint or use the printed absolute command. Plugin-only installation
provides Codex integration assets; run the trusted runtime installer once to add the executable.

Start a new Codex session for skill discovery, then ask:

```text
$codex-monitor Watch /absolute/project/build-status.json in this conversation.
Keep unchanged observations quiet. Set up the receiver and show its status.
```

Choose a real regular file. The first sample establishes a silent baseline; later changes send metadata, not file contents. The skill helps configure the installed runtime; installation alone does not select a conversation or start a producer.

<details>
<summary>Manual setup instead of the skill</summary>

Set `THREAD_ID` to your exact existing conversation ID. Initialize a new state once; reuse existing state on subsequent runs.

```bash
codex-monitor init
codex-monitor doctor --thread "$THREAD_ID"
codex-monitor monitor create build --thread "$THREAD_ID" --file /absolute/project/build-status.json
codex-monitor serve
```

`serve` stays in the foreground. For persistent macOS operation use the installed runtime's `service install`; see [receiver operations](docs/OPERATIONS.md). On Linux use an OS supervisor appropriate to your host. Use the same `--state PATH` throughout when changing the default `~/.local/state/codex-monitor`.

</details>

See [installation and upgrades](docs/INSTALLATION.md), [file monitor lifecycle](docs/CONVERSATION-MONITORS.md), and [external source setup](docs/SESSION-WORKFLOW.md). A public repository is not a package-registry release.

## Watch the connections

From a second terminal, with the receiver running:

```bash
codex-monitor dashboard
```

The **Live** dot pulses while the view refreshes. Green, red and amber status dots distinguish
configuration and observed health. Use `--no-animate` or `--color never` when preferred.

Select a conversation with the arrow keys and press **Enter** to open its ordinary Codex
TUI. Read events and responses, send messages, or answer approvals; exit the TUI to return to the
dashboard. Opening uses the binding's explicit shared owner endpoint and the same conversation ID.
`shared-local` bindings need an explicit owner endpoint before they can be opened this way.

The Graphite view shows one row per conversation, status dots, connection counts and recent activity.
The selected route appears below the list; use **Tab** to cycle routes before opening it.
Press **d** for technical details such
as conversation IDs, owner endpoints and receipt states. Green/red dots describe the displayed
binding or receiver; they do not imply that an agent is currently generating or has finished work.

Use `--once` for a snapshot, `--once --json` for structured output, or `--thread "$THREAD_ID"` to filter a conversation. The display refreshes observations without calling Codex or the model. See [dashboard controls and status meanings](docs/DASHBOARD.md).

The inventory covers bindings in the selected local state, not every Codex agent or every host. External producer health stays **unknown** until a supported observation exists. A running receiver, an accepted event and a completed work request are different facts. Native consumption remains an explicit `inspect DELIVERY_ID` check.

### Are monitors related?

Monitors are grouped by their **destination conversation**, not a parent agent. One conversation can
receive several monitors, and one receiver can serve several independent conversations. A PM or relay
conversation is optional: choose it when you want coordination, or route each source directly to its
project conversation. Sharing a receiver does not create a team or broadcast events to every binding.
See the [domain vocabulary](CONTEXT.md).

## What can I use it for?

| Goal | Setup | When the conversation receives input |
|---|---|---|
| Keep coding while a build finishes | Managed status-file monitor | Observed file changes after the initial baseline |
| Alert on sustained failure and recovery | JSON pointer predicate plus debounce | A matched or recovered condition stays stable |
| Collect updates from several workers in one PM conversation | Authenticated worker producers and selected bindings | Workers report relevant blocked, failed or completed work |
| Keep separate project conversations available | Independent monitors and exact conversation IDs | Each project's events route to its configured conversation |
| Relay requests from a messaging service | External service producer, optional relay, explicit reply adapter | A relevant incoming request arrives |
| Track requested work beyond delivery | Correlated request lifecycle | Explicit progress and terminal state transitions |
| Recover a service only when unhealthy | OS timer + included HTTP probe example + authorized recovery policy | Confirmed outage; healthy checks and recovery stay quiet |

Managed file collectors, predicates and request tracking are implemented. CI, Discord and other service-specific integrations require adapters; the core does not bundle a Discord bot. Source-side filtering prevents routine progress chatter from waking the model. See [copyable skill requests and usage details](docs/EXAMPLES.md).

For “check the server without spending chat tokens, then ask Codex to repair it only on failure,” see
the [OS timer health-hook recipe](docs/HEALTH-HOOK.md). The timer runs a script, not a model prompt.

## How it compares

Comparison checked **2026-09-09**. “Native Codex” means local CLI/Desktop capabilities without this project; supported hosted app-event tasks are called out separately. Claude entries describe official documentation, not a matched benchmark. Availability and experimental interfaces can change.

| Capability | Native Codex | Codex + codex-monitor | Claude Code |
|---|---|---|---|
| Human and agent interaction | Native conversation and task UI | Keeps those same conversations; adds routed external input | Native conversation; Channels can add external input |
| External event ingress | App Server integration APIs; hosted app-event tasks have a separate surface/plan scope [O1, O2] | Authenticated HTTP sources and managed file collectors, addressed to existing local conversations | Opted-in Channels deliver MCP notifications into a session [C1, C2] |
| Scheduled work | Desktop scheduled tasks, including return to an existing chat; web also has supported app events [O1] | Event waiting does not schedule model prompts; use native scheduling for time-based work | `/loop` schedules prompts in-session; Desktop scheduled tasks and cloud Routines are separate modes [C3, C7, C8] |
| Idle monitoring cost | Depends on the mechanism; a scheduled model check is a run | Receiver/collector/dashboard wait without model turns; actual event handling uses the model | Channel transport waits outside model processing; handling events uses the model [C2] |
| Events while answering | Native input lifecycle owns processing | Queues without interrupting the current turn | Channel events queue; busy arrivals may be handled together on the next turn [C2] |
| Delivery latency | Depends on integration and client | Shared-local scan roughly 10s in tested version; direct-owner CLI option measured separately | Push notification transport; busy state and processing still add delay [C2] |
| Closed local client | Local scheduled work requires the app running; cloud work has different hosting [O1] | Running receiver retains events; native queued input can wait for reopening | Channels require a running session; offline retention is an adapter responsibility [C1] |
| Multiple agents | Native subagents, model configuration and activity UI [O3] | Multiple registered conversations/sources; optional relay; no replacement agent scheduler | Subagents plus experimental teams with shared tasks and mailboxes; Channels are separate [C5, C6] |
| Monitoring visibility | Native agent activity, `/agent`, hooks and scheduled-task views [O1, O3, O4] | Read-only connection dashboard, receipts and managed collector observations; no native monitor badge | Source-labelled channel input, `/mcp` server status and selectable team panels [C1, C6] |
| Background monitoring | Native tools and lifecycle hooks; behavior depends on the integration | Managed collectors and external producers run independently of model turns | Native `Monitor` streams script output or WebSocket events while conversation continues; availability restrictions apply [C9] |
| Monitor startup/lifetime | Depends on the tool or integration | Installed receiver can outlive the client; source setup remains explicit | Plugins can declare auto-start monitors; session/subagent end stops its monitors [C9] |
| File conditions | Implement with tools, scripts or integrations | Managed sampling, JSON comparisons, stable debounce and recovery events | Implement through scripts, hooks or a channel server; not the same managed collector contract |
| Delivery recovery | Integration-specific; App Server exposes client primitives [O2] | Durable inbox, stable IDs, bounded retries and explicit uncertain-state inspection | Channel transport write is not model acknowledgement; durability belongs to the adapter [C2] |
| Outbound replies | Tools/integrations and task permissions | Explicit source-scoped outbox; an adapter retrieves and sends replies | Channel servers expose reply tools; permissions and routing depend on the channel [C2] |
| Request completion | Agent/task results | Explicit request states; queue consumption never implies successful work | Agent/task results; channel receipt alone is not work completion |
| Hooks | Lifecycle scripts and MCP handlers, with trust review [O4] | Can receive events emitted by an authorized hook; does not replace native hooks | `async` returns context next turn; `asyncRewake` can wake an idle session on exit 2 [C4] |
| Permissions | Native sandbox and approvals | Preserves native permissions; external input cannot authorize tools | Native permissions; optional channel permission relay supports selected remote approvals [C2] |
| Setup and support | First-party product | Additional runtime + skill, experimental queue dependency, adapter setup as needed | Channels research preview: per-session opt-in, allowed plugin and account/org requirements [C1, C2] |

Sources: [O1: scheduled tasks](https://learn.chatgpt.com/docs/automations), [O2: App Server](https://learn.chatgpt.com/docs/app-server), [O3: subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [O4: hooks](https://learn.chatgpt.com/docs/hooks), [C1: Channels](https://code.claude.com/docs/en/channels), [C2: Channels reference](https://code.claude.com/docs/en/channels-reference), [C3: session schedules](https://code.claude.com/docs/en/scheduled-tasks), [C4: hooks](https://code.claude.com/docs/en/hooks), [C5: subagents](https://code.claude.com/docs/en/sub-agents), [C6: teams](https://code.claude.com/docs/en/agent-teams), [C7: Desktop schedules](https://code.claude.com/docs/en/desktop-scheduled-tasks), [C8: cloud Routines](https://code.claude.com/docs/en/routines), [C9: Monitor tool](https://code.claude.com/docs/en/tools-reference#monitor-tool).

**Native Codex already supports visible agents and scheduled work.** This project's advantage is its reusable external-event delivery, persistence and observation layer for existing local conversations. Claude already provides native background monitoring through `Monitor`, and Channels offers session push with reply tools. There is no evidence for an overall reliability or speed superiority claim. See [detailed comparison and limitations](docs/PRODUCT-COMPARISON.md).

## What has been verified?

The CLI resident path now has a bounded real test with two ordinary TUI-created conversations:
closed-TUI event handling, same-owner reconnect, owner-down backlog retention and server restart
with both subscriptions restored. All four test events appeared once in native history and received
one response each. This does not establish unattended approvals, long-idle residency or OS reboot
recovery; Desktop owner access remains a separate limitation.

The core targets **Codex CLI 0.153.4** and experimental queue APIs. Evidence includes ordinary macOS/Linux TUI interaction, a one-hour TUI soak, a one-hour receiver outage test, Desktop consumption and user-observed restart, and installed runtime checks. These results have different scopes; they are not blanket platform certification.

Desktop draft/approval cases, Windows/WSL, OS sleep/reboot, fresh Desktop skill discovery and broader producer supervision remain open. Dashboard verification is recorded separately from Codex conversation-delivery tests. See [status](docs/STATUS.md), [test scope](docs/TESTING.md), [compatibility](docs/COMPATIBILITY.md), and [public evidence](docs/evidence/README.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check-publication.py
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before publishing. Keep raw transcripts and credentials out of Git; update status and the backlog with material changes.
