# Status and remaining verification

Status date: 2026-09-08. The default delivery path targets Codex CLI 0.153.4
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
Claude has been established; native UI, producer persistence, and Windows/WSL
remain gaps.

## Public repository

Repository: <https://github.com/Jaemani/codex-monitor>

Keep [TODO.md](TODO.md) current with code changes. Public evidence contains
summaries; raw conversations, terminal logs, and credentials remain local.
GitHub Actions run macOS/Linux Python 3.11/3.14 regression and packaging
checks on pushes and pull requests. See the [CI workflow](https://github.com/Jaemani/codex-monitor/actions/workflows/ci.yml).

## Next application: Discord project PM

The proposed next target is a supervised single-project Discord relay, followed by continuous worker-state monitoring. [DISCORD-PM.md](DISCORD-PM.md) defines operating levels and acceptance gates. This is a roadmap update, not evidence that a Discord integration or managed collector has shipped.
