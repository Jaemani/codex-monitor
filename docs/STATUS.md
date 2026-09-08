# Status and remaining verification

Status date: 2026-09-09. The default delivery path targets Codex CLI 0.153.4
through its official `shared-local` queue. An independent stdio writer places
input in the saved conversation store at the same `CODEX_HOME`/`sqlite_home`;
the CLI or Desktop native consumer owns and processes the conversation. The
writer does not load or start a conversation, resume a thread, start a turn,
or interrupt a turn. Direct WebSocket, daemon, and SSH adapters require the
target thread to be loaded on their server.

## Confirmed

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
