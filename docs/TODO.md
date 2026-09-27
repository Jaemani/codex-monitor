# Project TODO

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


Process lifecycle follow-up (2026-09-25):

- [x] Replace the partial multiprocessing-start ownership gap with an owned subprocess transport.
- [x] Add a receiver-wide startup/protocol failure circuit and visible recovery guidance.
- [x] Exercise real child early exit, post-spawn setup failure, timeout, and parent death.
- [ ] Add harness-to-receiver leases for detached canaries, including abrupt harness death.
- [ ] Expose process spawn/reap counters and runtime identity in service diagnostics.
- [ ] Automate retained-interpreter provisioning and pre-upgrade runtime validation in the installer.


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


Updated: 2026-09-12. The goal is reliable external-event participation in the user's ongoing Codex conversation, with ordinary CLI TUI as the primary interface and Desktop also supported.

This file is the project backlog. Update it with the corresponding code change; record measured results in STATUS.md and the public evidence summary. Do not mark a platform or user flow done from protocol tests alone.

## GitHub issue index

Synchronized **2026-09-10**. Every unchecked item below maps to an open issue. Completed milestones
stay in this document and STATUS.md; no retrospective issues were created just to inflate completion.
Implementation, measured validation and blocked integration are distinct states. Close an issue only
when its remaining acceptance criteria pass. Update the corresponding issue and this backlog together.

| Issue | Scope | Current state |
|---|---|---|
| [#1](https://github.com/Jaemani/codex-monitor/issues/1) | Event latency | Direct-owner measurements exist; broader validation remains |
| [#2](https://github.com/Jaemani/codex-monitor/issues/2) | Producers, adapters and webhook deployment | Managed files implemented; external integration/health remains |
| [#3](https://github.com/Jaemani/codex-monitor/issues/3) | Skill onboarding and Desktop interaction | CLI basic workflow passed; final-candidate and Desktop cases remain |
| [#4](https://github.com/Jaemani/codex-monitor/issues/4) | Unloaded Desktop ownership | Blocked on supported integration; not permanently impossible |
| [#5](https://github.com/Jaemani/codex-monitor/issues/5) | CLI resident operation | Bounded native checks passed; supervision, approvals and endurance remain |
| [#6](https://github.com/Jaemani/codex-monitor/issues/6) | Large-history reconnect | Mitigation implemented; root cause and deployment recovery unverified |
| [#7](https://github.com/Jaemani/codex-monitor/issues/7) | Versions, platforms and OS lifecycle | Partial matrix; Windows/WSL, SSH and recovery coverage remain |
| [#8](https://github.com/Jaemani/codex-monitor/issues/8) | Escalation policies | Planned beyond verified predicates/debounce |
| [#9](https://github.com/Jaemani/codex-monitor/issues/9) | Request archival | Planned beyond verified request lifecycle |
| [#10](https://github.com/Jaemani/codex-monitor/issues/10) | Versioned distribution | Final-candidate validation/publication pending; depends on #11 |
| [#11](https://github.com/Jaemani/codex-monitor/issues/11) | License | Owner decision pending |
| [#12](https://github.com/Jaemani/codex-monitor/issues/12) | Upgrade and rollback | Existing staging works; workflow and rollback verification remain |
| [#14](https://github.com/Jaemani/codex-monitor/issues/14) | Rust runtime adoption | Isolated candidate; matched evaluation and parity gates pending |
| [#13](https://github.com/Jaemani/codex-monitor/issues/13) | Matched Claude benchmark | Planned; current comparison is documentation-based |

- [x] **Dashboard duration and transient notices (2026-09-13).** Use whole
  paired elapsed-time units; clear previous action/open notices on navigation
  in both dashboards. Python route/row behavior passed real PTY regression.

## Rust candidate evaluation

- [x] **Isolated binary distribution and macOS service lifecycle (2026-09-12).**
  Versioned installation, checksum-before-execution validation, guarded rollback,
  immutable service binary, explicit start/stop/restart/remove and state
  preservation pass local tests and nine actual launchd checks. An asynchronous
  restart race was reproduced and fixed.
- [ ] **Rust service startup and migration.** Automatic login/reboot registration,
  Linux supervision and deliberate Python-state migration remain open. Current
  prefix-local launchd registration is session-scoped. ([#14](https://github.com/Jaemani/codex-monitor/issues/14))

- [x] **Full candidate sweep (2026-09-12).** macOS/Docker Rust and Python suites,
  matched functional contracts, installed wheel canaries and ordinary Codex
  0.154.0 Rust TUI passed. Fixed test-driver remote-resume flags and failure
  cleanup; platform/endurance/adoption gaps remain. See TESTING.md.

- [x] **Installed explicit-owner health (2026-09-12).** Python dashboard uses
  bounded read-only probes and distinguishes missing authentication, unavailable
  owners, unloaded tasks and unverified shared-local targets. Installed rollout,
  222 regressions and dashboard/control PTYs passed. Account presence does not
  prove valid credentials or successful model work.
- [ ] **Model failure telemetry.** Surface fresh execution/authentication failures
  beyond account presence without creating health-check turns. ([#14](https://github.com/Jaemani/codex-monitor/issues/14))

- [x] **Request CLI and dashboard follow-up (2026-09-12).** Durable expiry,
  ordered transition notifications, inspectable history and restart replay pass
  local regressions. Live terminal checks cover queue-only Enter guidance and
  pause/resume. Installed Python remains unchanged; broad UI acceptance remains.
- [x] **Native alias subscription repair (2026-09-12).** Register the worker-resolved
  parent as well as the configured parent; repeated symlink-path changes now wake
  before the fallback interval on macOS.
- [x] **Request HTTP foundation (2026-09-12).** Source-authenticated create,
  list, inspect and update endpoints now use indexed scoped lookups and bounded
  keyset pagination. Cross-source/thread isolation and lifecycle contracts pass. ([#14](https://github.com/Jaemani/codex-monitor/issues/14))
- [x] **Remove resident health-test timing race.** Hold the simulated outage
  through observation, verify recovery without resuming again, and clean up the
  worker even after an assertion failure. Found by the candidate's hosted CI.
- [x] **Isolated Rust core and matched evaluation.** A separate receiver, durable
  store, reusable native file workers, transports and CLI controls are implemented
  on `codex/rust-runtime`. Local contracts, matched functional cases, resource
  observations and ordinary TUI/resident restart checks are documented; this is
  not full feature or release parity.
- [ ] **Rust adoption gate.** ([#14](https://github.com/Jaemani/codex-monitor/issues/14))
  Develop and measure the isolated `codex/rust-runtime` candidate. Preserve durable
  delivery and ordinary TUI behavior; compare settled Python/Rust workloads before
  adopting it. Final request HTTP acceptance, dashboard acceptance and
  distribution/upgrade parity remain required. See [evaluation](RUST-EVALUATION.md).

## Technical progression

Prioritize [conversation-scoped monitoring levels](MONITOR-LEVELS.md). Local file collectors now run per conversation with checkpoint recovery. Next come broader failure isolation, condition policies and request tracking. Discord is a later integration example, not the core milestone.

## Next: reliable everyday use

- [x] **Review monitoring feedback against implementation and official sources.** Qualify the
  experimental API in the introduction, distinguish telemetry from event delivery, expand the
  comparison and clarify dashboard scope. See [review decisions](FEEDBACK-REVIEW.md); remaining
  source/contract/benchmark work is tracked in #2, #7 and #13.

- [x] **Scannable feature comparison.** Use support symbols and short labels with footnotes for
  runtime, Desktop, adapter and verification limits.

- [x] **Desktop-requested CLI operating guide.** Explain execution ownership, status checks, TUI access,
  supervision, route controls and the remaining unloaded-Desktop boundary.

- [x] **Optional consumer readiness gate.** `doctor --require-consumer` rejects unknown consumer
  readiness while preserving the default queue probe. Shared-local cannot verify the Desktop owner;
  this diagnostic does not complete the unloaded-conversation milestone below.

- [x] **Group assignment in agent setup guidance.** Inspect existing metadata, assign a known project
  and role for new conversations, and verify it before reporting setup complete. Unknown projects
  remain explicitly Ungrouped; this is guidance, not automatic project inference.

- [x] **Explicit project groups and conversation names.** Persist project/display metadata rather
  than inferring project identity from route prefixes. Group rows, distinguish duplicate labels,
  and leave unassigned conversations under Ungrouped. See [grouping](DASHBOARD.md).
- [x] **Scoped dashboard monitor controls.** Stop/resume the selected route; confirm removal while
  preserving receipts and the native conversation. Managed file collectors follow the same
  lifecycle updates. These controls do not restart external producers or revive unloaded clients.


- [x] **Global command registration.** The default runtime installer owns a stable
  `~/.local/bin/codex-monitor` link; `link` registers an existing install without restarting it.
  Custom prefixes require an explicit command directory. Foreign commands and modified owned links
  are preserved; shell configuration is unchanged. Plugin-only installation still requires runtime setup.
- [x] **Graphical connection overview.** Conversation-grouped rows, selected details, green/red/amber
  status dots and a persistent animated live header replace repeated diagnostics in the main view.
  Color and animation can be disabled. Live refresh is distinct from producer/model health; monitor
  grouping does not invent agent ancestry. See [dashboard guide](DASHBOARD.md).

- [x] **Read-only multi-conversation terminal dashboard.** `dashboard` provides a live summary and
  scrollable details, plus text/JSON snapshots and conversation filtering. It separates receiver
  readiness, delivery configuration, collector observations and explicit request states. It uses
  bounded read-only queries without model calls. See [dashboard scope](DASHBOARD.md) and the dated
  verification in STATUS.md. Native badges, unregistered agents and cross-host discovery remain
  outside this initial implementation.
- [x] **OS timer health-hook example.** A small HTTP probe emits only after consecutive failed checks,
  persists outage IDs across failed sends, and stays quiet after acknowledgement and on recovery.
  Local HTTP/inbox and real probe-process/receiver checks passed. Actual timer deployment and
  model-driven recovery require a selected service and authorized procedure; see [recipe](HEALTH-HOOK.md).

- [x] **Event-first session guidance.** Explain optional relay conversations, full-envelope forwarding,
  concise destination replies and separate configuration/process/delivery evidence. Updated local skill
  installed; this does not establish fresh-client behavioral validation.
- [ ] **External producer health integration.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Surface observed connection state and freshness from
  authenticated producers. Receiver liveness, REST credentials and an old successful receipt must not
  imply a currently connected event stream. Preserve unknown until observation exists.

- [x] **Conversation-scoped local file monitors.** Create/list/status/pause/resume/remove use the exact conversation. Identical names in two conversations, persistent checkpoints, receiver-owned sampling, installed process isolation and actual ordinary TUI event/follow-up checks passed. The managed Desktop event also arrived in the same conversation after the preceding assistant turn ended; pixel and approval cases remain unverified.


- [ ] **P1 — Manage event producers ([#2](https://github.com/Jaemani/codex-monitor/issues/2)).** Managed local files now expose observed collector state and checkpoint recovery under the receiver. Bounded process isolation passed installed failure/recovery checks. Remaining: broader CI/webhook producers, longer endurance coverage and OS sleep/reboot. A configured binding must not imply an active watch.
- [ ] **P1 — Conditional monitor policies (level 4).** ([#8](https://github.com/Jaemani/codex-monitor/issues/8)) Durable debounce and bounded JSON predicates passed installed macOS/Linux process checks, including invalid samples, restart/pause timing reset and independent conversations. A 300-second unchanged-condition soak passed. Actual ordinary default shared-local and Unix remote TUI matched/recovery events and user follow-up passed with exact client-ID correlation; escalation policies remain future work. Keep evaluation outside the model.
- [x] **Correlated request lifecycle foundation (level 5).** Durable request identity, explicit state transitions, expiry, ordered notification outbox and scoped CLI/HTTP passed installed macOS/Linux and ordinary TUI checks. Delivery acceptance never implies completed work. See [request workflow](REQUEST-LIFECYCLE.md).
- [ ] **P2 — Request retention and archival.** ([#9](https://github.com/Jaemani/codex-monitor/issues/9)) Provide explicit bounded archival with a documented deduplication horizon; preserve unresolved native delivery evidence. Current records are retained up to the store capacity.
- [ ] **P1 — Reduce event latency through supported APIs ([#1](https://github.com/Jaemani/codex-monitor/issues/1)).** Managed monitors now accept explicit owner endpoints. A six-sample actual Unix `codex --remote` TUI run passed: idle 0.793–1.150 seconds, post-receiver-restart 0.585–0.633 seconds, busy generation 30.005–33.051 seconds. Two samples per scenario are not a dependable tail estimate. Broader workloads and Desktop owner paths remain. See [technical latency comparison](LATENCY.md). Shared-local currently observes external changes at roughly 10-second intervals; do not claim universal real-time delivery or use private Desktop IPC.
- [ ] **P1 — Verify skill onboarding in fresh clients ([#3](https://github.com/Jaemani/codex-monitor/issues/3)).** Fresh ordinary TUI native skill autocomplete passed, and the installed basic skill workflow passed in an owned CLI TUI for create/status/pause/resume/remove on one disposable managed file. Remaining: rerun against the final candidate runtime and skill, fresh Desktop discovery, a real producer, and natural-language reply/stop coverage without changing the target conversation.
- [ ] **P1 — Complete Desktop interaction cases.** ([#3](https://github.com/Jaemani/codex-monitor/issues/3)) Unsent draft, event during active response, approval waiting and cancellation recovery; retain actual user-visible observations.
- [ ] **P2 — Add an external adapter after conversation-scoped collector lifecycle is verified.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Authenticate the producer, filter relevant changes, preserve stable event IDs, and implement explicit source-scoped replies.

## Compatibility and resilience

- [x] **Graphite conversation overview.** One compact row per conversation, uniform height, selected-row
  background and emerald marker, persistent route context, and Tab cycling between exact routes.
  The panel stays within 96 columns and keeps its footer next to the content.
  Small terminals retain route and exit controls. The initial selection prefers an enabled explicit
  owner route; shared-local opening remains an explicit error. See [visual QA](../design-qa.md).

- [x] **Calmer dashboard presentation.** Status dots, readable managed names, aligned activity,
  compact counts and explicit technical details passed installed PTY and same-thread TUI checks.
  The display does not infer native agent activity from enabled bindings.
- [x] **Dashboard-to-TUI interaction.** Enter/o opens the selected binding's existing conversation
  on its explicit owner endpoint and returns after TUI exit. Source and installed ordinary-TUI
  runs verified same-thread history and user interaction; regression covers routing, identity
  changes, authentication and terminal restoration. Shared-local owner discovery remains separate.
- [ ] **Large-history resident recovery.** ([#6](https://github.com/Jaemani/codex-monitor/issues/6)) Registration now omits saved turns from resume
  responses with `excludeTurns: true`. Verify recovery on the reported deployment before closing
  the incident; the suspected WebSocket frame limit has not been confirmed by a measured response
  or close code. Do not replay its original inputs to test subscription recovery.
- [x] **CLI owner subscription primitive.** `resident` registers explicit existing tasks on one
  owner, retains the connection and restores subscriptions after transport loss. `connect --thread`
  returns to the exact same task through that owner. Read-only probes do not create model turns.
- [ ] **CLI resident operational validation.** ([#5](https://github.com/Jaemani/codex-monitor/issues/5)) Complete real ordinary-TUI multi-task, disconnected
  UI, owner restart, human approval, long idle and OS supervision cases. Two actual TUI-created tasks,
  closed-UI delivery, same-owner reconnect and owner-down backlog/restart now pass bounded native
  checks. Remaining: human approvals, long idle, OS startup/sleep/reboot and dashboard integration
  for resident health. A foreground command alone is not automatic startup after reboot.

- [ ] **P1 — Unloaded conversation ownership.** ([#4](https://github.com/Jaemani/codex-monitor/issues/4)) Shared-local persistence cannot automatically load
  arbitrary Desktop tasks. A real accepted/queued event with a `notLoaded` target exposed this operational
  gap. Keep its receipt intact; actual single consumption and substantive reply remain unresolved.
  Implement a supported persistent-owner lifecycle without forced turns, replay or model polling.
  Same-owner subscription is required; an isolated native test confirmed competing-writer rejection.
  See [required architecture and acceptance criteria](OWNER-LIFECYCLE.md).
  Prior multi-conversation delivery results do not establish unloaded-task wakeup.

- [ ] **P1 — Version compatibility probe and matrix.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Structured doctor diagnostics now distinguish missing queue methods and consumer readiness. Actual isolated Codex 0.147.0 rejects `thread/queue/list`; 0.153.4 remains the tested baseline. Broader versions and affected Windows-host recovery remain unverified.
- [ ] **P1 — Windows/WSL.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Validate runtime transports and ordinary TUI. Current POSIX installer refuses Windows; do not advertise installer parity.
- [ ] **P2 — Desktop remote/SSH.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Test an explicitly configured remote host and exact conversation ownership.
- [ ] **P2 — OS sleep and reboot.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Verify receiver/producer restart, credential preservation and same-conversation event recovery.
- [ ] **P2 — Public webhook deployment.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Validate a selected reverse proxy, authentication, body/rate limits and outage recovery.
- [ ] **P2 — Matched Claude comparison.** ([#13](https://github.com/Jaemani/codex-monitor/issues/13)) Use the same event workloads, restart/failure cases and latency measurements. Document adapter differences; no overall superiority claim before evidence.

## Distribution and maintenance

- [x] **First hosted CI passed.** macOS/Linux × Python 3.11/3.14 regression and packaging: [CI workflow](https://github.com/Jaemani/codex-monitor/actions/workflows/ci.yml). This does not replace real-client UI tests.
- [ ] **P2 — Publish a versioned release.** ([#10](https://github.com/Jaemani/codex-monitor/issues/10)) Rebuild and verify the final archive, document dependencies and checksums, and decide release support policy. A public repository is not a package-registry release.
- [ ] **P2 — Choose a license.** ([#11](https://github.com/Jaemani/codex-monitor/issues/11)) Public source visibility alone does not grant an open-source license.
- [ ] **P2 — Upgrade/rollback usability.** ([#12](https://github.com/Jaemani/codex-monitor/issues/12)) Simplify receiver migration across runtime releases while preserving explicit ownership and state.

## Completed baseline

- [x] Official shared-local delivery into an existing conversation without starting/resuming threads or interrupting turns.
- [x] Actual ordinary TUI user/event/user interaction, draft preservation, idle silence and offline queue consumption.
- [x] macOS/Linux TUI baseline, Unix remote TUI, one-hour TUI soak.
- [x] Desktop same-conversation delivery and user-observed restart/event visibility.
- [x] Durable inbox, deduplication, bounded retries, uncertain-delivery inspection and explicit source-scoped reply outbox.
- [x] Receiver lifecycle, crash/restart and one-hour unavailable-endpoint validation.
- [x] Human-readable events, attach/sessions/pause/unpause/reply and honest unknown producer/target status.
- [x] Runtime installer and skill/plugin assets; isolated macOS/Linux archive lifecycle tests.
- [x] 151 regression tests in hosted macOS/Linux CI and official skill/plugin format validators.

Raw transcripts remain local. See [public evidence](evidence/README.md) and [compatibility](COMPATIBILITY.md).

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
