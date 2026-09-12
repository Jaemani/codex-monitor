# Status and remaining verification

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
Fresh logout/model-error telemetry and an installed health correction remain
outstanding. The Rust follow-up below does not claim to repair authentication
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
