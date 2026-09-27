# Status and remaining verification

## Native Discord producer cutover and publication (2026-09-27)

Both deployed Discord producers now use the installed Rust executable, in addition
to the native receiver and two residents. Read-only REST verification confirmed
bot identity, guild and every configured thread before cutover. Fresh health
snapshots confirmed both live Gateway connections ready after history recovery.
All 77 and 12 existing adapter receipt IDs were retained, with no raw backlog at
verification. The first launchd registration attempt failed and restored the old
producer; bounded registration retries after unloading completed the cutover.
Backups, service definitions, local paths and raw transcripts remain outside Git.
No synthetic Discord message or model turn was sent. The shared Codex owner and
active conversations were not restarted. This is connection/recovery evidence,
not an end-to-end reply or long-duration stability claim.

The native adapter preserves scoped routing, author restrictions, bounded image
attachments, durable delivery retries and receipt reactions. Optional legacy
`request_lifecycle` observation is rejected explicitly and was disabled in both
deployments. Existing one-shot project reply CLIs remain separate integrations.
See [Discord Gateway operations](DISCORD-GATEWAY.md) for configuration, differences,
restart recovery, rollback and credential handling.

A local legacy dashboard was reopened with Rust. An idle remote legacy view and
verified unused historical test processes were stopped normally. One legacy
Python dashboard still parents an open Codex TUI and is deliberately retained
until that client exits; terminating it would violate the active-work constraint.
The installed launch command already resolves to Rust for its next launch.
The dashboard handoff and return now say **Back to codex-monitor Dashboard**;
Codex's own `/quit` command-picker text belongs to the upstream TUI.

Validation: all 61 Rust tests, strict Clippy, formatting and 322 legacy Python
reference tests pass. Eight native Gateway tests cover authorization, configuration
rejection, schema preservation, image signatures, durable intake/restart,
receiver retry identity, and Gateway sequence/resume behavior using local fixtures.
The legacy doctor tests now isolate their state from the developer's live adopted
configuration. Installation, operation and sampler-safety docs distinguish the
native runtime from historical Python behavior. Publication checks pass.

A final receiver check observed four cumulative sample errors. The count remained
four while successful sample observations advanced from 8,446 to 8,516 over
20 seconds, with readiness retained. All seven active input files were readable;
configured JSON predicates parsed successfully at inspection. The historical
errors are not attributed to a specific watch by the current aggregate counter,
so this observation must not be described as error-free production operation.

Remaining: retire the protected legacy dashboard when its Codex client returns;
optional lifecycle-observer parity, longer soak and end-to-end Discord reply
verification are not claimed by this cutover.

## Post-adoption stability and resource check (2026-09-27)

The installed Rust 0.2.0 receiver and both residents were verified against the
native release executable. The receiver remained ready with zero sample errors
through 2,240 samples. This is short-run operational evidence, not a long-duration
soak or end-to-end Discord delivery certification. Existing pending/uncertain
records remain; receiver readiness does not establish consumer readiness.

A disposable fake-owner canary passed a 120-second outage, receiver SIGKILL and
restart, recovery with exactly one submission, duplicate receipt reuse, and a
second restart without replay. No operational conversation or model was invoked.
Black-box comparison passed 61 checks per runtime: HTTP intake/auth/deduplication,
pause/resume, restart persistence, predicate recovery and conversation isolation.
Known representation differences remain, including aggregate rather than per-watch
Rust sample errors; these results do not claim complete surface parity.

Matched collector comparison used seven 4,077-byte files, a two-second interval,
60 seconds unchanged and 60 seconds after changing every file, per runtime.
Python is the frozen legacy source in this working tree, not an exact replay of
its previous installed interpreter/release. Rust is the installed native binary.

| Process-tree RSS / CPU | Legacy Python | Rust 0.2.0 |
| --- | ---: | ---: |
| Unchanged RSS median | 20.3 MiB | 30.1 MiB |
| Unchanged RSS peak | 67.2 MiB | 38.8 MiB |
| After-change RSS median | 17.6 MiB | 27.4 MiB |
| After-change RSS peak | 65.4 MiB | 31.1 MiB |
| Whole-run waited CPU | 25.83 s | 0.44 s |

Both generated exactly seven change events, no unchanged events, preserved all
seven checkpoints and exited cleanly. Rust reduced sampled peaks and CPU, but its
RSS medians were higher in this run. The host was under substantial memory
compression and concurrent workload; sequential RSS samples cannot establish a
physical-memory or leak conclusion. Sampling can miss short-lived Python workers.
Owner/App Server, resident, dashboard and external-tool memory are excluded from
this matched collector comparison. Raw reports remain outside Git.

Remaining verification: longer production soak, memory footprint comparison under
controlled host pressure, and production permission changes/end-to-end delivery.

## Canonical Rust adoption (2026-09-27)

Rust is the authoritative runtime. Recent dashboard, owner-error, reconnect,
permission and resource-observation work is implemented natively in `rust/`.
The dashboard keeps project cards, explicit error details, width-based action
wrapping and a separate shared-server permission menu. Snapshot refresh and server
operations run outside the UI input loop. Disabled legacy routes do not affect
active permission summaries. Resident policy overrides verify returned permissions.

Native installation, receiver service management and offline Python-state migration
are available. Migration preserves IDs, uncertain/accepted states, reply records,
request history, watch baselines and credentials; original databases and private
backups remain available. Receiver locks and pending watch events block unsafe
migration. Python sources are frozen legacy reference, not a parallel runtime.
`headroom` remains optional; this adoption does not change model/provider settings.

Validation: 53 Rust tests and strict Clippy passed; a production-state copy
preserved all binding/event columns exactly. A real PTY verified six actions on one
row, shared-server scope and clean exit. No Discord message or model turn was sent
for these checks. Native macOS cutover is complete: receiver and both resident
services run the installed Rust executable. Receiver health reports ready/rust;
watch sampling reports no errors and no new events from unchanged baselines.
All 65 routes, 224 events, 43 watches, 20 metadata rows and one reply were migrated.
Installed binary and skill content match the canonical source release. The native
archive passed all 12 manifest hashes and an isolated installation, with no wheel.
Existing permission modes and model/provider configuration were preserved.
Linux supervision and production full/read-only switching are not claimed.

## Dashboard readability refinement (2026-09-27)

Detail actions occupy one horizontal row when the terminal has enough width;
wrapping depends only on available space, with no three-button limit. The dialog
can use up to 112 columns. All six actions remain visible at narrower widths.
Permission summaries ignore disabled legacy routes when active routes exist.
The overview no longer repeats `(configured)`: saved-setting provenance and the
lack of live verification are explained in details. Unknown access and conflicting
connections display as Access unknown and Check permissions.
Validation: 92 focused dashboard, permission-status and reconnect tests passed,
including wide single-row buttons and disabled-route aggregation regressions.
No live permission was changed and no Discord message was sent for validation.

## Permission labels and visible actions (2026-09-27)

Renamed the network-enabled workspace UI label to Project Access with an explicit
project-files/internet explanation. All detail actions, including Change Permission
and Close, wrap into visible rows instead of paging. Permission behavior is unchanged.
Validation: 89 focused tests passed, followed by 14 board tests after explanatory
copy changes. All action hit targets remain present at widths 40 through 120.
Rendered layout and installed UI hashes were checked; publication checks passed.

## Separate permission menu and status (2026-09-27)

Reconnect retains the current permission mode. Change Permission opens a separate
menu showing the saved policy and Full Access, Read-only, Workspace + Network and
Back choices. The same mode must be activated twice to preview then apply it.
Overview and detail labels show configured permissions without a native resume;
Unknown/Mixed remain explicit. Effective verification remains part of application.

Validation: 88 focused tests passed. A disposable PTY with a fake action backend
verified menu entry, mode selection/confirmation, Back/Esc, and ordinary Reconnect
without a permission override. Installed runtime hashes match source. No live
permission was changed to test these UI changes.

## Permission reconnect controls (2026-09-27)

Added explicit Full, Read-only and Workspace+Net reconnect actions with shared-owner
previews, mode-specific two-action confirmation, queue/idle gates, service backups,
rollback attempts and effective permission verification. Settings persist until
changed. Mandatory setup preflight/user choice now includes Discord thread/channel
creation. Existing explicit authorization is reused; full access is not mandatory.
The account-manager handoff describes scope, restoration and verification limits.

Validation: 314 tests passed in 54.505 seconds, including mode-specific confirmation,
queue blocking, backup/rollback and permission mismatch checks. Disposable native
App Servers returned the expected policy for all three modes without model turns.
The installed runtime matches source. Existing live owner permissions were not
switched for testing; operational defaults remain workspace-write with networking.
Skill validation and publication checks passed. Reopen an existing dashboard
process to load the new controls.

## Provisioning permission choice (2026-09-27)

The setup skill now distinguishes ordinary network-enabled replies from worktree,
routing and service provisioning. Before expanding access, identify the exact
missing scope and obtain a user choice unless already authorized: targeted access,
temporary full access with restoration, or keep restrictions. External resource
creation waits until required local setup is feasible. The setup guidance is complemented by the permission reconnect controls above. No live permission change was made for
this documentation update.

## Workspace networking repair (2026-09-27)

Resident now accepts `--sandbox workspace-write --network-access` and verifies
both effective fields after resume. Explicit network denial is also supported.
No automatic escalation or model/provider change occurs. Removed ineffective
dashboard/connect sandbox forwarding; remote clients use the owner policy.
Discord documentation now uses workspace restrictions with outbound networking,
not a full-access default. This supersedes the earlier full-access workaround.

Installed validation: all seven available conversations report workspaceWrite with
networkAccess true and idle status after owner/resident restart. Authenticated
Discord bot/channel GETs succeed under that effective policy, while a write
outside the workspace is denied. No Discord message or model turn was submitted.
The existing missing-provider conversation remains unresolved. Owner and resident
service definitions were backed up locally; global Codex configuration and account
credentials were not changed. The ineffective owner bypass flag was removed.
The full suite passed 289 tests in 55.121 seconds. Publication checks passed.

## Corrected permission root cause (2026-09-27)

A disposable real-server reproduction isolated legacy sandbox restoration from
concurrent clients. Missing named permission state caused resume to derive the
trusted-project workspace default with network access disabled. Enabling network
access alone retained workspace restrictions and passed authenticated Discord
GET verification. Full access is not required. Earlier client-race explanations
are superseded by [the root-cause report](RESUME-PERMISSION-ROOT-CAUSE.md).
Remote TUI flag forwarding is not proof of an applied remote permission policy.
The diagnosis itself did not change live settings; the narrower repair below supersedes the workaround.

## Optional provider handling (2026-09-27)

Reconnect no longer rejects every custom provider. Built-in OpenAI needs no
custom entry; other saved provider IDs are checked against effective model_providers
from config/read in each conversation's own project directory. Missing or
unverifiable definitions still block restart. No provider is required by name,
installed automatically, or substituted silently. Configuration presence does not
validate a custom provider's credentials or model execution. This supersedes the
blanket custom-provider restriction described in the earlier reconnect entry.

Validation: 101 focused reconnect, auth, dashboard and RPC tests passed, including
17 reconnect tests. Cases cover default OpenAI without custom configuration,
an optional configured provider, per-project absence and redacted config-read
failure. A live read confirmed that the unresolved task refers to a provider
absent from its effective project configuration. No login or provider was changed.
See ACCOUNT-MANAGER-INTEGRATION.md for the independent account-manager handoff.

The updated wheel is installed and receiver readiness and source/installed backend
hashes passed. Monitor credentials and Codex configuration were preserved. A
disposable real App Server config/read probe retained an optional provider passed
as a command-line override; it made no model call and changed no user config.

## Reconnect with the current saved account (2026-09-27)

The Python dashboard now replaces Retry auth with a two-action Reconnect flow.
The preview enumerates conversations sharing a verified local macOS LaunchAgent.
Confirmation verifies the current saved login and saved tasks in a short-lived
unsubscribed App Server, rechecks idle states and resident coverage, and restarts
only the exact loaded owner job. A PID/account/access/loaded-thread verification
follows; timeouts after restart acceptance do not trigger another restart.
Active tasks, changed job configuration, custom/unknown saved providers and
missing resident coverage block restart. A cross-dashboard lock prevents races.
No account tokens are copied and no failed inputs or model turns are submitted.

The real owner adopted the current saved account and seven original conversations
returned idle. An eighth could not resume because its saved custom model provider
was absent from current configuration. It remains unresolved pending the user's
choice of provider; no provider fallback was silently applied. This live result
is partial conversation restoration and successful account API access, not proof
of model execution. The missing-provider case now has a pre-restart guard and
regression coverage. Raw operational logs remain outside Git.

Verification: 279 source tests passed in 55.478 seconds. Thirteen reconnect
regressions cover account changes, busy/uncovered tasks, stale plans, missing
providers, concurrent dashboards and restart-command timeout reconciliation.
A disposable PTY verified preview, explicit second activation, wrapped results
and clean exit with a fake backend. The initial PTY driver stopped draining
output before exit and timed out; the corrected driver drains through exit.

The versioned wheel upgrade is installed. Source/installed hashes for the
dashboard, reconnect backend and RPC transport match. Receiver readiness,
configuration/credential preservation and a read-only seven-conversation
reconnect preview passed. Existing dashboard processes must be reopened.

## Manual authentication retry and native error details (2026-09-27)

The Python dashboard now shows the newest failed turn's redacted error message
for explicit owners reporting systemError. A one-turn, item-free history read
keeps the request bounded; unsupported history is reported explicitly.
Retry auth is an explicit background action: one managed-token refresh followed
by an account-access check, one action in flight and a 30-second interval per
dashboard instance. It does not restart owners, switch accounts or replay work.
Failed work must still be inspected and retried in the native Codex client.

Validation: 266 source tests passed in 55.267 seconds, including 64 focused
owner/dashboard checks. A disposable real PTY verified visible native error text,
keyboard activation, wrapped recovery results and clean exit against a fake owner.
A read-only live probe retrieved the reported logged-out/account-changed refresh
error on three existing routes. No operational credentials were refreshed and no
model turn was started; this is not evidence of repaired login or completed work.
Rust dashboard parity and automatic authentication recovery remain out of scope.

The wheel was installed through the versioned upgrade path. Installed dashboard
modules match the source; receiver readiness and preserved configuration and
credential files were verified. The installed reader retrieved the same three
native authentication error details. Reopen existing dashboards to load the UI.

## Project-scoped resource presentation (2026-09-26)

The shared resource panel has been removed. Each project card reports only its explicitly
owned monitor sampler workers, their sampled RSS and CPU. No shared receiver CPU or global
CPU is shown on the dashboard. The authenticated supervisor maps worker PIDs to conversation
IDs; OS samples must confirm the receiver as parent before attribution. Missing ownership or
samples display Unknown. Zero active samplers does not mean an idle Codex agent.
Agent-owner CPU/RAM and shared database/log storage cannot currently be attributed per project;
the detail overlay states this limitation rather than dividing shared totals arbitrarily.
Shared resource data remains available in diagnostic JSON; process-safety warnings remain.
This supersedes the shared panel presentation described below.


## Resource and delivery observations (2026-09-26)

The dashboard includes the oldest unresolved event age and per-route queue-acceptance
median from the latest 100 accepted events. Failed, uncertain and retrying counts are
separate from completed work. Reply completion latency remains explicitly unknown.
A resource panel reports receiver-tree process count, sampled zombies, RSS and CPU,
sampler occupancy/limit and spawn suspension, plus state-directory DB/log/logical bytes
and change per hour over the displayed measurement window. The receiver reports its
own PID and supervisor state through authenticated status. Detached agent owners,
dashboard processes, watched files and installed runtimes are outside this scope.

Process/storage observations are cached for 30 seconds. The single ps child is owned
by subprocess.run with a one-second timeout and reaping; failed collection is unknown,
not zero. Directory scans have an entry/time budget and skip symlinks. No model calls,
telemetry history files or alert messages are created. A point sample can miss short-lived
workers/zombies; CPU is the operating system ps value, and aggregate RSS may count shared
pages more than once. Process-limit headroom and restart/spawn rates are not yet collected.
Resource panels appear at 32+ rows; resource alerts remain visible in shorter views.
Zombie alerts require two consecutive samples; a single exited child awaiting reap
is still shown in the sampled count but does not imply a leak.


## Terminal dashboard rebuild (2026-09-25)

Replaced the list-plus-inspector presentation with a status summary, attention panel and
responsive bordered project panels. Enter/click opens a bounded overlay; Esc restores the
unchanged overview. Arrow keys navigate spatially, and visible actions replace memorized
management shortcuts. Details retain route selection, delivery and explicit work reports.
Mouse reporting is disabled during Codex handoff and on exit. Snapshot/JSON diagnostics remain
separate. This supersedes the earlier expanding-inspector layout described in historical entries.

Verification: all 248 Python tests and 40 focused dashboard tests pass, including bounded rendering, reachable spatial
navigation, pointer targets, terminal input parsing and existing identity-safe action tests.
Refresh retains thread and route identity; disappearing routes close the overlay. Cell-aware
wrapping preserves Korean diagnostic text, and detail scrolling is bounded.
Actual terminal inspection covers the overview, Enter-to-details, mouse inspection and Esc return.
The rebuilt wheel is installed locally and the managed receiver was restarted. This is UI
verification, not evidence of successful model execution or repaired authentication.

## Dashboard execution visibility (2026-09-25)

Implemented conversation-specific bounded owner probes, explicit `systemError`
attention, enabled-route wording, and oldest unresolved delivery warnings after
one hour. Queue acceptance remains separate from execution success. Paused
routes no longer override active owner health. No automatic replay or restart.

Remaining: producers must report request completion/failure and remote reply
outcomes; the dashboard does not validate credentials with a model call or send
out-of-band alerts. Long-lived dashboard processes must be reopened after upgrade.


Sampler ownership correction (2026-09-25): replace multiprocessing startup with
explicitly owned subprocesses, retain bounded child accounting through reap, and
stop all new sampler admission after startup/protocol failure. A broken runtime
can no longer bypass the worker registry through a failed multiprocessing
bootstrap. Receiver startup cleanup now also covers initialization/output errors.
Validation: 229 tests in the clean full suite, then 28 final focused sampler tests;
all 17 installed-wheel isolation checks passed. No new model or authentication test.
See [process safety](PROCESS_SAFETY.md) for recovery and verification boundaries.
Private incident transcripts and host-specific runtime evidence remain outside Git.


Desktop setup restriction (2026-09-14): refuse monitoring on the requesting Desktop conversation,
including attempts to transfer that same session to CLI. New setup must use an authorized new,
separate CLI monitoring task with a distinct thread ID and verified owner/resident readiness.
A new monitor definition targeting the same Desktop task is not a workaround. Existing explicitly
selected CLI monitors remain manageable; legacy Desktop routes remain available for diagnosis and
scoped disabling, without automatic migration, pause, deletion or replay. This is skill guidance,
not a runtime-level caller-surface block or a fix for Desktop ownership.
Historical Desktop delivery evidence does not establish supported unattended operation.


Dashboard route inspector (2026-09-13): overview separates active/paused registrations
and recent delivery. Details browse every route, show source/file, owner/collector
observations, delivery history and receipt IDs, with wrapped text and scrolling.
Auto-refresh is static and separate from health; queue-only routes explain TUI limits.
Existing routes and operational state are not changed by this display update.


Dashboard usability correction (2026-09-13): elapsed time uses whole paired
units (`3d 2h`, `5h 12m`, `1m 1s`) instead of decimal days/hours. Navigation
clears prior route/action notices in Python and Rust. The original stale open
notice was reproduced in a PTY before the fix; 23 Python dashboard tests and
44 amended live dashboard PTY checks pass afterward. The corrected wheel was
installed locally; existing dashboards need reopening to load the change.

Operational recovery and candidate work (2026-09-12): two already-loaded CLI
routes were reconnected from shared-local to their existing explicit owner
without changing task IDs, source allowlists or receipt identities. Six configured
CLI targets were observed loaded/idle with empty queues; one other target retained
an active-writer conflict and was not taken over. Receiver, owner, resident and
external gateway processes were running. These observations do not prove future
credential validity or task success. Private target details and backups stay local.

The Python dashboard now uses bounded cached read-only explicit-owner probes.
Missing required authentication, unavailable owners and unloaded tasks no longer
appear as ready. Shared-local targets stay unverified. Rust source-scoped request
HTTP APIs and an isolated versioned binary installer are implemented. Nine
actual disposable macOS service lifecycle checks passed after fixing a launchd
restart race; stop/remove wait for unload and restart uses `kickstart -k`. Desktop cold-task work is excluded
from this recovery scope, and Rust state migration remains a separate adoption gate.


Installed recovery verification (2026-09-12): the corrected Python wheel was
upgraded through the versioned installer and its receiver LaunchAgent restarted.
Authenticated receiver readiness passed after startup; all 49 binding identities
remained present. Existing operational activity continued during the rollout;
two managed routes changed enabled state independently, so a frozen inventory
is not claimed. Six explicit CLI targets remained ready to receive. The
dashboard distinguishes account presence from credential validity/model success.
The source passed 222 Python tests, 42 dashboard PTY checks and 22 control PTY
checks. Reopen an existing dashboard with `q`, then `codex-monitor dashboard`
to load the updated code. No owner, resident or external gateway was restarted.


Rust recovery candidate (2026-09-12): the final source passed 36 Rust tests,
strict Clippy and formatting, including actual fake-owner CLI/RPC health checks
and honest multi-route status-dot aggregation. The rebuilt release passed 19 ordinary Codex 0.154.0 TUI checks in
34.585 seconds, including closed-UI delivery, receiver restart, owner restart,
exact-once native observations and same-conversation reopening. Six Python
installer/service tests and nine actual isolated macOS launchd checks passed.
The initial launchd restart failure is retained in local evidence. Prefix-local
service registration is session-scoped; automatic login/reboot registration,
Linux supervision and cross-runtime state migration remain unverified/open.

Full candidate verification (2026-09-12): macOS and Docker Linux both passed
29 Rust contracts and 208 Python checkout regressions. The final macOS rerun
passed all 211 tests including three new cleanup regressions. Matched functional checks
passed 61 cases per runtime. The Rust release passed 19 real Codex 0.154.0 TUI
checks after correcting the canary's remote-resume permission flags and cleanup.
Installed-wheel process/predicate/request and monitor dashboard checks passed.
See [the dated matrix](TESTING.md#full-candidate-verification-2026-09-12) for exact
scope, initial failures, pending Python changes and excluded acceptance gates.
At that earlier verification point no installed service or default runtime was replaced;
the later Python health rollout is recorded above.


Rust candidate update (2026-09-12): CLI requests now expose history and durable
status notifications, with automatic expiry, replay deduplication, per-request
ordering and capacity deferral. Paused routes do not block unrelated notices;
removed routes and expired delivery receipts settle notices explicitly. Idle
maintenance checks do not write or invoke a model. Live dashboard terminal checks
cover queue-only Enter guidance, pause/resume and clean exit. macOS native
subscriptions now include worker-resolved directory aliases; repeated changes
through a symlink pass before the 30-second fallback. These are candidate checks,
not installed rollout or full Codex TUI/desktop acceptance. HTTP request parity,
authentication-aware owner telemetry, distribution and endurance remain open.


Known operational health gap (2026-09-12): enabled routes and queue acceptance
must not be presented as proof of working Codex authentication or execution.
Receiver availability and owner/model readiness are separate observations.
The installed explicit-owner health correction is now recorded above. Fresh
model-error telemetry beyond account presence remains outstanding. The Rust follow-up below does not claim to repair authentication
or reconnect an owner.

Status date: 2026-09-09. The default delivery path targets Codex CLI 0.153.4
through its official `shared-local` queue. An independent stdio writer places
input in the saved conversation store at the same `CODEX_HOME`/`sqlite_home`;
the CLI or Desktop native consumer owns and processes the conversation. The
writer does not load or start a conversation, resume a thread, start a turn,
or interrupt a turn. Direct WebSocket, daemon, and SSH adapters require the
target thread to be loaded on their server.

Documentation update (2026-09-10): Desktop-requested CLI monitoring now has an explicit setup guide,
ownership boundaries, user handoff commands, status semantics and scoped stop controls. The source
and local skill guidance were updated. No runtime behavior or new Desktop verification is claimed.

Documentation update (2026-09-10): the README comparison now separates event features, availability
and controls into symbol-based support matrices with linked caveats. Existing source/evidence scope
is preserved; no new comparative benchmark or platform verification is claimed.

Issue synchronization (2026-09-10): GitHub issues #1–#3 now distinguish completed evidence from
remaining acceptance criteria. Issues #4–#13 cover previously document-only open work; every unchecked
TODO item links to an issue. Desktop ownership is an integration blocker, large-history reconnect is
an unverified mitigation, and CLI residency still needs operational coverage. No issue was closed
from documentation changes, and no new implementation or test evidence is claimed.

Feedback review (2026-09-10): the introduction now qualifies the experimental App Server queue,
and the comparison distinguishes command/WebSocket monitoring, plugin startup and OTel execution
telemetry. Dashboard copy now names delivery observations and explicit work reports; JSON declares
its scope without claiming live model/tool telemetry. Twenty-two focused tests passed in 2.780 seconds;
the final disposable dashboard PTY run passed 42 checks in 6.386 seconds. This is dashboard-only
source validation, not new native delivery, Desktop, runtime upgrade or matched Claude evidence.
An auxiliary system-Python full-suite attempt had three errors from a missing websockets dependency;
the focused checks used the project virtual environment. See [review decisions](FEEDBACK-REVIEW.md).

Rust candidate (2026-09-10): implementation is isolated on `codex/rust-runtime`,
with its own executable, database and credentials. The Python installation and
main branch remain unchanged. The candidate uses native file notifications,
bounded reusable workers, a persistent SQLite connection and bounded native/HTTP
connections. Adoption requires matched resource measurements and workflow parity;
see [Rust evaluation](RUST-EVALUATION.md) and issue #14. Prior Python evidence does
not establish Rust support. Local checks include 20 Rust contracts, 206 unchanged
Python tests, 61 matched functional assertions per runtime and actual ordinary
TUI/resident/receiver/owner restart checks. Resource runs show lower RSS in the
valid 0/10-watch comparisons and bounded Rust behavior at 128 watches; overloaded
Python cases are excluded from relative performance comparisons. Detailed test
boundaries and remaining parity gaps are in the evaluation, not inferred from
protocol success.

Hosted Rust checks passed on macOS and Ubuntu. The first Python CI run passed
three matrix entries but exposed a timing-sensitive resident health test on
macOS/Python 3.14; its failed assertion left a worker running until job timeout.
The test now holds the injected failure until observation, checks recovery and
always stops its worker. No Python runtime behavior changed.

## Incident findings and current limitations (2026-09-09)

- Desktop follow-up: shared-local queue access passed for the current conversation, while the local
  owner endpoint remained unavailable. CLI and bundled Desktop binaries still report 0.153.4.
  Added optional `doctor --require-consumer`: unknown consumption cannot pass an unattended setup
  check. Fifteen doctor/CLI tests passed; a native read-only check passed four assertions in 0.917
  seconds using an isolated loaded owner and the current Desktop queue. No messages or model turns
  were created. This does not fix unloaded Desktop wakeup. Desktop UI automation was refused again.
  Two initial native harness runs exposed fixture assumptions (unmaterialized threads have no stored
  rollout/turn list); corrected checks passed and initial reports remain local.

- Setup guidance now includes explicit conversation group/name assignment and verification in the
  skill entrypoint. The dashboard guide previously documented grouping, but the installed skill
  omitted this setup step. Binding and managed-monitor creation still do not assign groups automatically.

- Explicit project groups and stable conversation names are stored separately from routing IDs.
  The dashboard now stops/resumes the selected route and confirms removal; retired routes remain
  in the audit store but cannot accept fresh events or dispatch new claims. Native conversations
  and receipts are preserved. The full source suite passed **201 tests in 54.706 seconds**.
  Installed controls: **22 PTY checks**; read-only refresh: **42 checks in 6.208 seconds**;
  ordinary TUI open/follow-up/return: **19.748 seconds**; managed sampler control check: **1.222 seconds**.
  Final confirmation/long-label polish passed 21 focused tests in 2.725 seconds and the final-wheel
  controls rerun in 1.183 seconds. Global upgrade matched 21 modules, retained eighteen bindings,
  twelve receipts and credentials, and assigned eight conversations to the requested project group.
  Receiver readiness returned. Resume does not restart external producers or revive unloaded clients;
  delivery already in flight may finish. No new Desktop validation is claimed.

- Bounded compact panel: live width is capped at 96 columns; keyboard hints follow the content.
  This prevents right-side indicators and the footer from drifting to distant terminal edges.
  Dashboard tests: 18 passed in 2.700 seconds; global-installed PTY: 42 checks passed in
  6.180 seconds. Sixteen bindings, ten receipts and credentials remained intact;
  receiver readiness returned. No additional native-delivery claim is made for this layout-only fix.

- Compact Graphite follow-up: removed bottom-only selection padding and automatic row expansion.
  All rows now have one-line height; selected context follows the inventory directly. Dashboard
  regression: 18 tests in 2.692 seconds. Installed PTY: 42 checks in 6.274 seconds; ordinary TUI
  route selection, same-thread interaction and dashboard return: PASS in 20.073 seconds.
  Global upgrade matched 21 modules, preserved sixteen bindings, ten receipts and credentials,
  and returned the receiver ready.

- Graphite dashboard, 2026-09-09: one row per conversation, adaptive spacing, emerald selection
  marker, full-width highlight and persistent route context. The source suite passed **194 tests
  in 53.730 seconds**; after the final footer fix, all 18 dashboard tests passed in 2.677 seconds.
  The final installed wheel passed **42 dashboard PTY checks in 6.119 seconds** and the ordinary
  TUI multi-route open/follow-up/return canary in **19.930 seconds**. Tab cycled shared-local and
  explicit owner routes; shared-local opening was rejected and the exact owner route opened.
  Global installation matched all 21 modules, preserved sixteen bindings, nine receipts and
  credentials, and returned the receiver ready. Existing open dashboards need to be restarted.
  Visual comparison used synthetic conversation data; no new Desktop or event-delivery claim.

- Dashboard visual polish replaces status labels with colored dots, uses managed monitor names,
  aligns recent activity and moves technical IDs/endpoints to details. The compact header reports
  counts and refresh age; receiver diagnostics remain visible in the pinned header when details
  are open. No-color dots use distinct shapes. Final source regression: **192 tests in 54.064 seconds**.
  The installed wheel passed **39 dashboard PTY checks in 7.779 seconds** and the ordinary-TUI
  open/follow-up/return canary in **20.091 seconds**. Global upgrade matched all 21 modules,
  preserved fourteen bindings, eight receipts and credentials, and returned the receiver ready.
  The final global-installed PTY rerun added palette coverage and passed **40 checks in 7.953 seconds**.
  Dot colors retain their original configuration/readiness meaning, not native model activity.
- Dashboard Enter/o now opens the selected existing conversation in the ordinary Codex TUI using
  its saved explicit owner endpoint. `/quit` returns to the dashboard. Refreshes remain read-only;
  shared-local routes cannot infer an owner, and changed/unsafe identities are rejected. The source
  native run passed in **25.878 seconds**; the installed-wheel run passed in **18.634 seconds** with
  same-thread user follow-up, dashboard return and owned-fixture cleanup. Final regression:
  **191 tests in 51.146 seconds**; installed dashboard PTY: **39 checks in 7.728 seconds**.
  The wheel was installed globally and all 21 modules matched. Receiver readiness returned after
  upgrade, preserving twelve bindings, seven receipts and credentials. Other owner/resident services
  were not restarted. This adds no Desktop wakeup or original-incident recovery claim.
- Desktop owner-access recheck: both installed client binaries report 0.153.4, the Desktop child
  exposes no observed listener, and `doctor --endpoint local --surface desktop` still fails.
  Official async hooks do not start a turn on completion; MCP/settings/Remote documentation supplies
  no verified arbitrary local owner attachment or unloaded-task wakeup API. This is a documented
  integration gap, not a new delivery failure or proof of permanent impossibility. See
  [the follow-up and source links](COMPATIBILITY.md#owner-attachment-follow-up-2026-09-09).
- Resident registration now requests `excludeTurns: true` on `thread/resume`, retaining the
  subscription without transferring the complete saved history. A deployment reported repeated
  resume disconnections on a large existing task; logs and upstream source identify unnecessary
  history hydration, but do not establish a measured frame-limit failure. Recovery of that actual
  task has not been verified, and its services and queued inputs were not changed for this patch.
  Regression: **185 tests in 51.917 seconds**. The isolated source resident/ordinary-TUI rerun
  passed in **72.701 seconds**, covering two tasks, reconnect and a **23.0-second** owner outage.
  All four test events produced one native input and response each. This is not a large-history
  reproduction, installed-service validation or Desktop fix.
- CLI-first owner lifecycle is implemented: `resident` retains explicit
  conversations on one shared owner and restores subscriptions after connection loss;
  `connect --thread` opens that exact task in the ordinary remote TUI. Registration is a deliberate
  lifecycle operation, distinct from delivery retry. Default local Desktop owner access remains
  unresolved. Foreground owner/resident processes currently need separate OS supervision.
- A bounded actual ordinary macOS TUI run passed in **26.800 seconds** using Codex 0.153.4 and
  Luna/xhigh. One selected conversation consumed an event once while its TUI was closed, accepted
  a user follow-up after reopening, and consumed another event once after owner restart and resident
  re-registration. This is not multi-task, long-idle, approval-screen or Desktop evidence. Earlier
  incomplete/timed-out attempts remain recorded in the curated evidence.
- Review fixes add per-target transient registration retry, native-shareable endpoint validation and
  honest failed-probe readiness. Known pre-submission outages no longer consume delivery attempts;
  expiry and uncertain-delivery reconciliation remain intact. The complete local suite passed
  **183 tests in 51.153 seconds** after these runtime changes.
- The matching resident wheel was installed locally with the updated skill. Authenticated receiver
  readiness returned after restart; all eight bindings, credentials and seven prior receipts were
  preserved. The original incident still has one submission attempt; no replay or Desktop recovery
  is claimed. A separate candidate install passed 51 focused tests and removed its owned command
  and skill cleanly. The global `resident` command also works from another working directory.
- Expanded native coverage passed in **75.617 seconds**: two TUI-created conversations, independent
  exact-once events while both TUIs were closed, same-owner transport reconnect, a **23.3-second**
  owner outage with a pending event consuming zero attempts, and owner restart restoring both
  subscriptions. Backlog and post-restart events each produced one native response. Both fixtures
  were archived and cleanup completed before PASS. The suite including report-safety regressions
  passed **185 tests in 50.880 seconds**. Long-idle residency, reboot and approval-screen cases remain.
- The final native rerun passed in **71.765 seconds**, including a **22.4-second** owner outage
  and the same four exact-once event/response checks across two tasks. The harness now distinguishes
  a temporary in-flight attempt reservation from a spent retry in settled pending state.
- Implementation `86042e0` passed all four hosted macOS/Linux Python 3.11/3.14 regression and
  packaging jobs: [CI run](https://github.com/Jaemani/codex-monitor/actions/runs/34316671221).

- A reported Windows health-hook delivery failure was traced to a missing `thread/queue/list` API
  in Codex 0.147.0. An isolated official 0.147.0 macOS binary independently reproduced JSON-RPC
  `-32600` / unknown method variant. The updated doctor diagnosed it without sending input or changing
  the host's Codex installation. Actual Windows upgrade and end-to-end recovery remain unverified.
- A real Desktop delivery remained `accepted`/`queued` while its target reported `notLoaded` and
  no new processing turn. The independent writer and Desktop returned matching latest turn IDs.
  Inspected upstream 0.153.4 source watches owned thread IDs and cannot auto-load an unloaded task.
  This establishes an operational limitation; the original receipt's single consumption and actual
  responder output are still **unresolved**. No replay, forced load/turn, interruption or database edit
  was used. Prior loaded/multiple-target tests do not cover unattended unloaded tasks.
- Diagnostics now separate queue API access from unknown consumer presence, report missing methods
  with the tested baseline, and explain accepted input waiting for native consumption. The protocol
  diagnostics are a usability fix, not an automatic owner-lifecycle implementation. See
  [troubleshooting](TROUBLESHOOTING.md) and [compatibility](COMPATIBILITY.md).
- The complete local suite passed **162 tests in 51.250 seconds**. Actual 0.147.0 incompatible and
  0.153.4 readable-target doctor checks passed. These checks do not establish event-response recovery.

- A follow-up isolated two-server native probe confirmed competing resume is rejected with
  `already has an active writer`. Source analysis also excludes background terminals as a residency
  guard. The default local Desktop owner connection remains the blocking integration boundary;
  [owner lifecycle](OWNER-LIFECYCLE.md) records required functionality and acceptance criteria.
- The diagnostics wheel and skill were installed locally with the receiver returned ready. All eight
  bindings, credentials and seven existing receipts were preserved. The original event remained
  queued with one submission attempt at the final inspection; no response recovery is claimed.

## Confirmed

- Graphical/global-command implementation `9b57d60` passed all four hosted macOS/Linux
  Python 3.11/3.14 regression and packaging jobs:
  [CI run](https://github.com/Jaemani/codex-monitor/actions/runs/34265996815).

- Graphical dashboard and global command update: the default installer now registers
  `~/.local/bin/codex-monitor`; `link` supports existing installs without a receiver restart.
  Foreign commands, modified owned links, legacy markers, opt-out and removal have focused coverage.
  The actual command worked from a different directory and a fresh zsh login shell, with no PATH
  shadowing on the tested host. Plugin-only installation still needs the runtime setup step.
- The dashboard now groups compact rows by conversation, provides selected details, green/red/amber
  labels and a fixed animated `LIVE VIEW` indicator with snapshot age. Animation does not increase
  read/probe frequency. Color and motion controls preserve plain snapshots and read-only behavior.
  Grouping represents destination routing, not a parent/child agent hierarchy.
- Final local regression: **157 tests passed in 50.153 seconds**. The installed graphical wheel
  passed **39 actual dashboard PTY checks in 7.789 seconds**, including animation, color, navigation,
  resizing, receiver outage/recovery and terminal restoration. Source PTY: 39 checks in 7.721 seconds.
  A separate 20-conversation render check verified last-row details and marker-like characters in
  conversation names. These are dashboard checks, not new Codex conversation or Desktop UI evidence.
- The tested wheel (SHA-256 `7cea50f597f40ad614f936df0dc5d6d7b678e9c0ac61ba8d58ef990ff3f67cdc`)
  was installed locally; all 19 runtime modules matched. Controlled receiver upgrade preserved all
  eight bindings, credentials and six pre-existing receipts. The service returned authenticated-ready
  after startup. The disposable candidate runtime, skill and owned command link also uninstalled cleanly.
  Initial post-bootstrap status preceded process readiness; subsequent bounded readiness verification passed.


- Implementation commit `dc06e0a` passed hosted macOS/Linux checks on Python 3.11 and 3.14,
  including the 151-test regression suite and release packaging:
  [CI run](https://github.com/Jaemani/codex-monitor/actions/runs/34263553188).
  The README comparison now also covers Claude's documented native `Monitor` tool for background
  scripts and WebSocket events, with availability and session-lifetime limitations.

- README now includes managed-file, stable-condition, multi-worker PM, optional relay and scoped
  status/pause examples, with external adapter prerequisites. A read-only multi-conversation terminal
  dashboard was subsequently implemented; current scope and verification are described below.
- `dashboard` implements a separate read-only terminal view with conversation filtering, scrolling,
  resize handling and text/JSON snapshots. It reads local database snapshots and checks authenticated
  receiver HTTP readiness without constructing a monitor store, Codex RPC client or model session.
  Source connectivity remains unknown; collector freshness and explicitly reported request state
  are separate observations. The final local regression suite passed 151 tests in 32.617 seconds, including seven dashboard
  regressions and three health-hook tests. The installed wheel matched all 19 runtime modules
  (SHA-256 `64e4d4e52f1fe7ebcd435c57ed7beff2b6a297aec19db3b1e00b11050cc84014`).
- The final installed-wheel dashboard PTY passed 31 checks in 8.118 seconds at the default
  two-second refresh interval: multiple conversations, exact filtering, full snapshots, scrolling,
  narrow resize, receiver outage/recovery, q/Ctrl-C and terminal restoration. Earlier failed reports
  are retained: the harness initially resolved the virtualenv interpreter to its base Python, and
  the actual macOS terminal retained PENDIN after TCSADRAIN. The harness preserves the venv path;
  runtime cleanup now flushes pending navigation input while restoring settings. The owned local
  runtime and skill were upgraded, the receiver returned authenticated-ready, and all eight bindings,
  credentials and previous receipts were preserved. External producer health remains unknown.
- The OS timer health-hook example passed three focused tests, including an actual probe subprocess
  sending through the CLI to an authenticated local receiver and a real HTTP health fixture. Healthy
  checks and recovery stay quiet; confirmed outages are deduplicated after acknowledgement loss,
  and a later outage has a new identity. These checks use a local delivery sink, not model repair.

- Session usability update: the skill now routes event monitoring away from scheduled model polling,
  explains optional relay conversations, preserves stored envelopes for authorized forwarding, and
  keeps routine replies free of transport diagnostics. The updated owned local skill was installed
  without restarting the receiver. Skill format validation passed. The final regression suite passed
  141 tests in 27.483 seconds. Readable session status now separates delivery configuration, receiver
  process liveness and unverified producer health, with a latest-receipt inspection command. It was
  checked against existing local state without mutation. The running installed receiver/runtime was
  not upgraded; the new CLI display is available in the checkout. Fresh-client natural-language
  behavior for this revision remains unverified.
- Read-only inspection of the user-reported external relay test confirmed an enabled binding,
  a running receiver process and native consumption of its test receipt. The supplied transcript
  reports PM forwarding and destination replies; those remote actions were not independently repeated.
  External producer health remains unknown, and producer-specific claim/delivered tracking is outside
  this core verification. Receipt consumption does not prove continued source availability.

- The final macOS regression passed 66 tests in 21.880 seconds, including five
  installer tests. Isolated archive checks covered hashes, relative-wheel
  installation, status, upgrade, failed-upgrade preservation, helper discovery,
  removal, and state preservation. The same archive passed installation,
  upgrade, and removal checks on Debian 12 arm64 with Python 3.11.
- The session UX wheel suites passed 61 tests on both macOS and Linux. They
  cover `attach`, readable `sessions`, `pause`/`unpause`, event rendering,
  source-scoped durable replies, duplicate/conflict handling, restart behavior,
  and no-model-call status operations.
- `inspect DELIVERY_ID` is implemented and validated in installed environments.
  It distinguishes local `accepted` from native `queued`, `consumed`, and
  `unknown` using a bounded, read-only lookup. It does not delete, start,
  resume, interrupt, or otherwise mutate the native queue.
- The real CLI TUI baseline and extended run passed, including active-turn
  contention, preservation of unsubmitted drafts, idle behavior, event storage
  during exit, and same-conversation resume. The extended run took 51.828
  seconds; a 60-second smoke soak also passed.
- The real TUI one-hour soak passed in 3,600.025 seconds with 12 events, three
  exit/restart cycles, 20 completed native turns, no duplicate delivery, and no
  idle history growth. The Unix `codex --remote` TUI baseline passed in 36.98
  seconds. Debian 12 arm64 passed nine native TUI checks in 33.481 seconds.
- Two real App Server `shared-local` canaries passed around user input, during
  active work, while idle, and after writer reconnect. History reconciliation
  found one client ID for duplicate input, and the writer loaded zero threads.
- A real Desktop conversation consumed a `shared-local` event. An explicit
  reply was stored separately; the originating source retrieved and
  acknowledged it, the local receipt remained intact, and each event client ID
  appeared once. Acknowledgement did not wake Codex. The user also observed a
  complete Desktop exit/reopen cycle with the event still visible. This does
  not include automated pixel inspection.
- The receiver passed an independent 3,600.15-second LaunchAgent outage run
  with 665 health checks and 55 normal restarts. Credentials, deduplication,
  operator decisions, and cleanup were preserved; the test job and plist were
  removed afterward.
- The stress run passed with 20 seeds, 2,000 unique events, and 6,000 ingress
  attempts. This is a fake-contract result and does not prove exactly-once
  behavior for all of Codex or HTTP.
- The macOS service canary passed install/start, idempotent start, forced
  termination recovery, restart, stop/start, reinstall, uninstall, and state
  preservation. The corrected run took 16.25 seconds; the Desktop-thread run
  took 16.46 seconds. The initial bootstrap race remains documented as a
  failure and regression case.
- The isolated native Unix WebSocket probe passed after disabling compression
  negotiation. `initialize` and `thread/loaded/list` succeeded and the server
  exited cleanly.

See [TESTING.md](TESTING.md) and the dated reports under `docs/evidence/` for
the evidence and exact scopes.

## Incomplete or unverified

- A separate one-hour receiver run ended without a completion report after
  3,542 seconds and 56 restarts; it remains **INCOMPLETE**. The later
  independent 3,600.15-second run is the PASS result. The first run's stop
  cause is unknown.
- Ctrl-C and approval-waiting behavior was tested. The first expectation of
  automatic queue processing was recorded as a failure; after a user sent a
  follow-up turn, the queued event processed exactly once. The monitor does not
  override user cancellation or approve work automatically.
- Desktop pixel automation, Desktop unsubmitted drafts, and Desktop events
  during approval waiting remain unverified.
- SSH remote Desktop projects, Windows/WSL, macOS reboot/sleep, an external
  public webhook reverse proxy, and model judgment during long real tasks
  remain unverified.

## Corrected assumptions and failures

- The original assumption that Desktop required a direct connection to the same
  App Server was replaced by the documented `shared-local` queue path and real
  consumer checks.
- The first macOS LaunchAgent run exposed a bootstrap race immediately after
  unload. Native restart and bounded re-registration retries fixed it; the
  original report remains as regression evidence.
- The native Unix-socket handshake initially failed because the client offered
  compression. The current implementation disables compression for that path.

Code tests, native consumer responses, screen visibility, and long-running
stability are separate evidence classes. Passing a fake or protocol check does
not close a screen or soak item. No overall compatibility or superiority over
Claude has been established; native UI, broader producer supervision, and Windows/WSL
remain gaps.

## Public repository

Repository: <https://github.com/Jaemani/codex-monitor>

Keep [TODO.md](TODO.md) current with code changes. Public evidence contains
summaries; raw conversations, terminal logs, and credentials remain local.
GitHub Actions run macOS/Linux Python 3.11/3.14 regression and packaging
checks on pushes and pull requests. See the [CI workflow](https://github.com/Jaemani/codex-monitor/actions/workflows/ci.yml).

## Current implementation milestone

Conversation-scoped managed file collectors now support create/list/status/pause/resume/remove, identical names in different conversations, durable checkpoints and receiver-owned sampling. [MONITOR-LEVELS.md](MONITOR-LEVELS.md) defines the progression; [CONVERSATION-MONITORS.md](CONVERSATION-MONITORS.md) documents the interface. Discord remains an application example.

The frozen managed-feature wheel passed 75 regression tests on macOS and Debian 12 arm64/Python 3.11, plus 16 installed receiver/collector process checks on both platforms. An actual ordinary macOS TUI rendered and consumed a managed file event, then completed a user follow-up response in the same conversation. A 60.054-second managed-collector soak passed with six events, one receiver restart and no duplicates. This does not extend the earlier one-hour transport/TUI result to the new collector.

The new managed Desktop event arrived as external input in this same conversation after the preceding assistant turn ended. The receipt matched the single previously accepted test event, which had remained queued during active work. This is direct conversation-arrival evidence; no new history inspection, pixel verification or business-task completion is claimed. No forced turn or duplicate test event was used.

At that milestone, general condition policies, correlated request lifecycles and workload isolation remained future work. The subsequent isolation milestone below replaces sequential in-process sampling with bounded child processes.

The managed-collector macOS LaunchAgent canary passed ten checks in 8.604 seconds: installation/readiness, silent baseline, changed-file receipts, SIGKILL recovery without duplicates, checkpoint preservation, stop and uninstall cleanup. This used the real installed service and a fake App Server peer; it is not Desktop or model-delivery evidence.

Managed implementation commit `f8df8ef` passed all four hosted macOS/Linux Python 3.11/3.14 regression and packaging jobs ([run](https://github.com/Jaemani/codex-monitor/actions/runs/34233805563)). The rebuilt local archive passed nine manifest hashes, equality of all 14 runtime payload files with the tested wheel, and isolated installer/skill install-status-uninstall with state preservation. The default local runtime and skill are installed; no permanent receiver was started. The archive remains unpublished.

Fresh ordinary TUI skill discovery subsequently passed through the native autocomplete list: typing `$codex-mon` displayed the selectable Codex Monitor skill. The earlier prompt-echo result remains invalid. This proves discovery only; natural-language workflow execution and fresh Desktop discovery remain unverified.

## Isolation and condition milestone (2026-09-09)

The current candidate moves watched-file reads into short-lived child processes with parent-death detection, per-receiver and per-conversation capacity limits, parent deadlines and lifecycle-epoch rejection of stale results. The receiver alone owns checkpoints and event intake. Managed monitors can use `--debounce` for durable stable-sample filtering; restart, pause/resume and failed observations restart the observed stability window.

The macOS source suite passed 100 tests in 24.023 seconds. The final installed isolation/condition canary passed 17 checks. The macOS LaunchAgent canary passed ten checks in 8.84 seconds, and archive install/status/uninstall plus state preservation passed. Debian 12 arm64/Python 3.11 passed all 100 installed-wheel tests in 23.649 seconds and the 17 isolation/condition checks in 25.175 seconds. The installed sampler completed 300.155 seconds with 30 events, one receiver restart and no duplicate hashes. Its combined TUI phase was skipped because the optional test driver dependency `pyte` was missing, so that combined report is INCOMPLETE. A separate real macOS TUI run then passed in 29.954 seconds using the same wheel: its event rendered and was consumed, and the subsequent user response appeared in native history. The original combined INCOMPLETE report and successful timed sampler phase remain preserved. Earlier 75-test and 60-second evidence above belongs to the preceding runtime.

The isolation canary initially timed out waiting for healthy change delivery. Review found that its new conversation IDs were rejected by the fake App Server fixture; that failure is not proof of a production sampler defect. The fixture was corrected and the same installed wheel passed all 17 checks. Independently, the sampler exit/response lifecycle was hardened so the child explicitly exits after its bounded result write and the parent never waits for a partial live-worker response. Failure evidence remains local. See [structural reliability limits](RELIABILITY-LIMITS.md) for kernel, storage and native-client limits.

Implementation commit `75700ba` passed all four hosted macOS/Linux Python 3.11/3.14 regression and packaging jobs ([run](https://github.com/Jaemani/codex-monitor/actions/runs/34242698063)).

The validated wheel was installed as an upgrade at the local default runtime path, and the bundled skill was refreshed. Existing monitor state remains outside the runtime prefix; no permanent receiver or new user monitor was started by the upgrade.

## Request tracking and explicit CLI endpoints (2026-09-09)

Tracked requests now preserve their original source, receipt and conversation,
with explicit revisions, acknowledgement, progress, terminal state and expiry.
Meaningful transitions enter a durable, ordered notification outbox. Quiet
acknowledgement, duplicate/CAS conflicts, pause deferral, restart replay,
notification ordering and request-worker failure isolation are covered. Queue
acceptance and consumption never mark a request completed automatically.

The frozen wheel passed 124 macOS source tests in 27.432 seconds and 124
installed-wheel tests on Linux/Python 3.11 in 28.112 seconds. The installed
request lifecycle canary passed 32 process/HTTP checks on Linux. Its enhanced
macOS run passed 37 checks in 43.616 seconds, including ordinary TUI draft
preservation, quiet acknowledgement, explicit completion rendered/consumed once,
and a native assistant response to subsequent user input. These do not establish
the truth of a producer's completion report or long-term business-task quality.

Managed file monitors can now persist explicit existing owner endpoints.
Creation and status remain offline operations; dispatch still requires the exact
thread to be loaded on a direct server. The installed managed remote canary
passed 19 checks in 32.474 seconds using an owned Unix App Server and actual
`codex --remote` TUI. A sampled change reached the visible TUI in 1.032 seconds.
This is one observation, not a latency bound or a change to shared-local timing.
The official App Server transport remains experimental.

The local archive passed checksum and runtime-payload comparison against the
frozen wheel, isolated installation, request CLI, uninstall and state preservation
in 3.660 seconds. Its first fixture inherited a different CODEX_THREAD_ID and
correctly failed the scope guard; the corrected isolated fixture passed. The
archive remains unpublished. Default request retention is bounded to 10,000
records; explicit archival remains future work.

The combined Linux run's sampler cleanup phase initially failed without a
container init process. A focused reproduction identified both remaining PIDs
as terminated zombies adopted by Python PID 1. The same wheel passed all 17
isolation checks with Docker `--init`, leaving no sampler PIDs. The original
combined FAIL is preserved; its successful regression and request phases are
reported separately. Container deployments need an init/reaper for orphaned
children after forced receiver termination.

Commit `154c257` passed all four hosted macOS/Linux Python 3.11/3.14 regression
and packaging jobs ([run](https://github.com/Jaemani/codex-monitor/actions/runs/34247881428)).
The validated wheel and bundled skill were installed as the next default local
runtime upgrade. Existing state was preserved and no permanent receiver was
started by the installer.

A follow-up HTTP hardening removes store-wide activity counters from
source-authenticated request responses; six focused API checks passed.

## JSON conditions and receiver compatibility (2026-09-09)

Managed JSON files now support typed equality and numeric comparisons through
bounded JSON pointers. Only stable changes between matched and not-matched
states produce events. Initial observations are silent; invalid observations
reset the stability window. Unrelated document changes do not reset a sustained
condition. Expected and selected values are omitted from status and events.

The current source suite passed 134 tests, including rejection of old active
receivers before predicate creation or request mutation, and durable event replay
when sampler startup repeatedly fails. The installed predicate soak passed 300.001 seconds with 30 unrelated JSON
writes and no additional events. A separate ordinary Unix remote TUI run
passed in 55.088 seconds: matched and recovered events rendered, each exact
client ID occurred once in native history, and a subsequent user turn received
a native response. The stricter rerun replaced broad trust-prompt detection
and state-text-only correlation in the earlier harness; the earlier reports
remain preserved. No new Desktop or business-task completion claim is made.

The replacement installed wheel passed 134 tests on Debian 12 arm64/Python
3.11 in 26.347 seconds, 32 request process checks in 6.070 seconds and 16
predicate process checks in 22.193 seconds. Docker used an init/reaper.

An artifact equality check rejected an outdated frozen wheel: four runtime
modules differed from the newer archive. Latest tests against that old wheel
also failed with two errors and one failure. Those reports remain preserved.
The replacement wheel was compared against all 18 source runtime modules.
Its archive then passed checksum, runtime equality, isolated install, request
and predicate CLI, skill removal and state preservation in 3.939 seconds.
The same wheel passed ten macOS LaunchAgent managed-file lifecycle checks in
8.941 seconds; this service test uses a fake App Server and adds no UI claim.

## Natural-language installed-skill workflow (2026-09-09)

An audited canary passed an ordinary macOS Codex CLI TUI workflow using the
installed `codex-monitor` skill. The test ran `gpt-5.6-luna` at `xhigh` in an
owned PTY with `workspace-write` and `on-request`, discovered one exact owned
thread, and sent natural-language `$codex-monitor` turns for create, status,
pause, resume and remove. It used a disposable watched file, monitor state,
receiver and test conversation.

The active file change and the change detected after resume each produced one
local `accepted` receipt and one read-only native `consumed` result keyed by a
stable client ID. A change while paused and a change after removal produced no
new receipt or native history item. The receiver and collector were checked
through authenticated HTTP and installed CLI status. The successful run took
323.225 seconds; its report and pyte capture are preserved under
[`docs/evidence/skill-workflow-2026-09-09/`](evidence/skill-workflow-2026-09-09/).

This result covers the installed basic managed-file workflow only. It does not
claim model task success, Desktop discovery, predicate policies, request
lifecycle behavior, or the final candidate source state. The run used the
installed `codex-monitor 0.1.0` launcher and the installed skill identified by
its owner marker and `SKILL.md` hash; candidate runtime or skill changes need
their own rerun. Earlier harness failures and aborted attempts remain
preserved in the same evidence directory. No command approval prompt appeared
and no command approval was answered.

Predicate implementation commit `7fdcdfc` was pushed, and the validated wheel
and updated bundled skill were installed as the default local upgrade. Existing
state was preserved; the installer did not start a permanent receiver. All four hosted macOS/Linux Python 3.11/3.14 regression and packaging jobs
passed ([run](https://github.com/Jaemani/codex-monitor/actions/runs/34252934328)).

## Bounded CLI latency observations (2026-09-09)

The six-sample ordinary Unix remote TUI run passed in 105.276 seconds. Each
event rendered and was inspected as consumed using its exact client ID. Two
idle samples took 0.793–1.150 seconds, two post-receiver-restart samples took
0.585–0.633 seconds, and two samples during a 1,000-word response took
30.005–33.051 seconds. Most busy delay occurred after native acceptance.
See [latency details](LATENCY.md) for observation boundaries and small-sample
limits. Immediate processing during an active user turn is not guaranteed.

Earlier failed harness attempts remain local. The final driver uses official
`inProgress` turn state instead of inferring activity from a visible composer
or footer. Five mocked regression checks cover report completion and cleanup
failures, ensuring a running or skipped required phase cannot be reported as
an overall PASS. No new Desktop validation is claimed.

The final source suite including the five report-lifecycle regressions passed
139 tests in 28.038 seconds. The runtime wheel is unchanged from the preceding
134-test installed validation; these new tests exercise verification scripts.

A sixth report-lifecycle regression subsequently passed with the focused
suite: terminal capture and thread archival failures still close all owned
resources, verify temporary-directory removal, and finalize the report as FAIL.

The same final predicate wheel also passed the ordinary default shared-local
CLI TUI in 55.757 seconds: matched and recovered events each had one exact
client ID in native history, both rendered, and a user follow-up received a
response. Workspace trust was observed; no command approval occurred. This
adds the default CLI path to the separate Unix remote result.

Latency commit `2914e1e` passed all four hosted regression and packaging jobs
([run](https://github.com/Jaemani/codex-monitor/actions/runs/34254314660)).
The workflow now pins official checkout v7.0.1 and setup-python v7.0.0 commits
to replace the deprecated Node 20 action versions.

The refreshed-action run exposed a timing-sensitive Ubuntu/Python 3.11 test
failure during healthy reconnect reconciliation. The test had reused its
intentional 0.1-second lost-response timeout for normal reconnect calls. The
short deadline now applies only to the deliberately dropped response; normal
startup and reconciliation retain their existing bounded test budget. The
focused test and 20 repetitions passed. Production timeouts are unchanged;
the failed hosted run remains recorded.

Expanded inspector readability: three ruled sections (current state, delivery,
connections), aligned short fields and one recovery action. Raw endpoints, receipt
IDs and duplicate caveats remain available in technical output, not the inspector.

Independent product and interaction reviews led to a compact operational overview:
separate attention/unchecked totals, explicit receiver health, three columns with
last activity (not claimed delivery), and a short selected summary. Column headers
and project identity stay pinned. The a key jumps to issues; connection mutations
require opening details first. Error routes are preferred when inspecting issues.

Resource-metrics verification (2026-09-26): 252 Python tests passed in 49.922 seconds;
publication checks passed. Installed runtime upgraded and authenticated receiver readiness
confirmed. Live macOS receiver-tree process/RSS/CPU/zombie and state-storage observations
were returned successfully. These are point samples, not a guarantee against future leaks.

Project resource attribution verification: 60 focused dashboard, sampler and resource tests passed.
The supervisor publishes ownership at each completed scheduling pass; busy status requests may
reuse it for at most five seconds. Older ownership is unknown. Installed locally and receiver restarted.
