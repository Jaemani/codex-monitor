# Test design and results

Results below are dated 2026-09-08. The primary environment was macOS with
Python 3.14.6, Codex CLI 0.153.4, and `websockets` 16.1.1. Linux wheel and
TUI checks used Debian 12 arm64 and Python 3.11 where stated.

The session UX wheel suites passed 61 checks on macOS and Linux. They cover
source-scoped replies, duplicate and conflict handling, restart behavior,
acknowledgement without a model call, and no-model-call behavior for
`attach`/`sessions`/`pause`/`unpause`. The real TUI display check passed in
38.676 seconds, and the LaunchAgent reinstall/recovery check passed in
17.39 seconds. A real Desktop conversation also consumed the new event format
and completed the explicit reply → source lookup → acknowledgement round trip.
That result does not include automated pixel inspection.

## Managed conversation monitors

The managed-feature wheel passed 75 tests on macOS (21.801 seconds) and Debian 12 arm64/Python 3.11 (20.661 seconds). Both platforms passed 16 installed receiver/collector process checks using a fake App Server peer. Cases include exact scope, identical names in two conversations, silent baselines, change routing, pause/resume/remove isolation, SIGKILL checkpoint recovery and FIFO rejection.

The actual ordinary macOS TUI rendered and consumed a managed file event and completed a subsequent user response in the same conversation. A separate 60.054-second collector soak observed six events, one receiver restart, eight unchanged samples and no duplicates. This is short-soak evidence for the new collector, not a new one-hour result. No business workflow completion is claimed.

```bash
python3 scripts/managed-monitor-canary.py --run \
  --python /absolute/installed-venv/bin/python \
  --soak-seconds 60 --tui --report /tmp/managed-monitor.json
```

The TUI option requires Codex and the optional `pyte` dependency. Unavailable TUI dependencies yield an incomplete result, never a UI PASS. Process checks without `--tui` do not verify a real client. The new Desktop managed event subsequently arrived as external input in the same conversation after the preceding assistant turn ended. Its receipt matched the previously queued test event. This proves conversation arrival, not pixel visibility or completed business work; no new source polling was needed.

## Test boundaries

The public verification surface is:

1. Authenticated HTTP event intake and CLI configuration/status APIs.
2. Durable inbox ingest, delivery, status, and recovery APIs.
3. Codex App Server JSON-RPC, queue, and history behavior.
4. Canaries sharing a thread with a real user client.

Deterministic tests use a fake App Server to reproduce protocol contention and
lost responses without model calls. They use explicit fixtures and temporary
state so they cannot accidentally reach a real CLI or inbox. Real checks
require `--run`. The private database layout is not a test contract.

## Cross-platform wheel regression: PASS

The first clean final-wheel regression passed 51 tests on both macOS and
Debian 12 arm64. After read-only inspection was added, the source suite passed
53 tests in 6.146 seconds and a separate macOS wheel passed the same 53 tests
in 6.132 seconds. The Linux wheel used for the real TUI also passed 53 checks.

Coverage includes:

| Area | Coverage |
|---|---|
| CLI and HTTP | Installation, permissions, authentication, source isolation, binding/status APIs, origins, size limits, concurrent duplicates, and overload. |
| Delivery | FIFO, malformed and expired input, conflicting IDs, retries, crash/restart recovery, uncertain delivery, and independent bindings. |
| Sessions | Same-thread delivery, preservation of user queue input, history reconciliation, bounded pagination/deadlines, unloaded-thread rejection, and no `thread/start`, `thread/resume`, `turn/start`, or interrupt calls. |
| Inspection | Read-only `queued`/`consumed`/`unknown` queue/history lookup with bounded scans and no mutation. |
| Transport | Unix and remote WebSocket handshakes, token-file validation, EOF/timeouts, blocked writes, cleanup, and reconnect. |
| Limits and process safety | Seeded duplicates/restarts/FIFO, trace and hop limits, rate and pending limits, binding disable, receiver locking, and SIGKILL lock release. |
| Watchers | Baseline suppression, change-only delivery, and replay after a lost receipt. |

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The Linux wheel run imported the installed package from outside the checkout;
it did not use credentials, model calls, or a TUI.

## Seeded stress: PASS

Twenty seeds produced 2,000 unique events and 6,000 ingress attempts, mixing
source order, duplicates, pre-delivery failures, lost post-delivery responses,
and monitor recreation. Duplicate external accepted effects were zero and all
unique events were accepted. This is a fake-contract result; it is not an
exactly-once guarantee for Codex or the whole HTTP path.

```bash
.venv/bin/python scripts/stress.py --seeds 20 --events 100 \
  --report /tmp/codex-monitor-stress.json
```

## App Server canary: PASS

The canary used a shared loopback WebSocket server and a disposable synthetic
conversation. It did not write to an existing user's conversation or an
operational inbox. It verified delivery before, during, and after user turns,
no extra idle turn, reconnect after monitor disconnect, one client message ID
for a duplicate webhook, queue/history confirmation, and no isolated-workdir
changes. The model instruction prohibited tools, file changes, and external
contact.

```bash
.venv/bin/python scripts/real-canary.py --run \
  --report /tmp/codex-monitor-real-canary.json
```

This is a model/protocol end-to-end check, not a CLI or Desktop screen check.

## `shared-local` and real clients

The independent stdio writer uses the official queue in the same Codex store;
it does not load a thread or call `thread/start`, `thread/resume`, or
`turn/start`. Two real App Server canaries passed, including user input around
events, an idle case, reconnect, one duplicate client ID, and zero writer
loaded threads.

The real CLI TUI passed its baseline and extended cases in a test-owned PTY:
user → event → user ordering, active-turn contention, preservation of an
unsubmitted draft, no idle turn, event storage during CLI exit, and resume in
the same conversation. The extended run passed in 51.828 seconds; a 60-second
smoke soak with three events and three TUI restarts also passed. A one-hour TUI
soak passed in 3,600.025 seconds with 12 events, three real exit/restart cycles,
20 completed native turns, no duplicates, and no idle history growth. The
Unix App Server `codex --remote` TUI baseline passed in 36.98 seconds.

The real Desktop checks confirmed native consumption, an explicit reply,
source-scoped lookup and acknowledgement, preserved local receipts, and
exactly one matching client ID. Pixel capture is separate. The event remains
visible in the conversation because the current path uses the native queue.
The user also observed a complete Desktop exit/reopen cycle with the event
still visible.

During approval waiting, external queue input did not approve work and no
sandbox command ran. The first Ctrl-C check was recorded as FAIL because it
expected automatic queue processing. A follow-up user turn then caused the
queued event to process exactly once. This is the intended interrupted-queue
behavior of Codex CLI 0.153.4; the monitor does not override cancellation.
See [CANCELLATION-QUEUE.md](CANCELLATION-QUEUE.md).

## Installed receiver lifecycle

`lifecycle-canary.py` must run from a fresh wheel environment outside the
checkout. It uses a real `serve` process and authenticated HTTP on a test
port. Coverage includes credential preservation, permissions, duplicate
receiver rejection, SIGKILL immediately after intake, restart deduplication,
dead/uncertain reporting, status and acknowledgement during an outage, clean
restart, and explicit operator discard. Acknowledgement success is not model
delivery success.

```bash
.venv/bin/python -m pip wheel . --wheel-dir /tmp/codex-monitor-lifecycle-wheels
.venv/bin/python -m venv /tmp/codex-monitor-lifecycle-venv
/tmp/codex-monitor-lifecycle-venv/bin/python -m pip install --no-index \
  --find-links /tmp/codex-monitor-lifecycle-wheels codex-monitor
# Run outside the checkout; use the absolute checkout path for the script.
/tmp/codex-monitor-lifecycle-venv/bin/python /absolute/path/to/codex-monitor/scripts/lifecycle-canary.py \
  --run --outage-seconds 3600 --report /tmp/codex-monitor-lifecycle-hour.json
```

The command above is a reproduction method, not a completed one-hour result.
The short 2026-09-08 run passed in 176.59 seconds with a 120.01-second outage,
23 health samples, one restart during the outage, SIGKILL recovery, and
operator discard recovery. The event reached `dead` after five attempts.
This check does not cover model responses, screen behavior, post-recovery
delivery, reboot, sleep, app restart, or long-running task quality.

An earlier one-hour receiver run ended without a completion report after 3,542
seconds and 56 restarts, so it remains **INCOMPLETE**. An independent one-shot
LaunchAgent run later passed after 3,600.15 seconds with 665 health checks and
55 normal restarts; its completion report is the PASS evidence.

## macOS service and Linux TUI checks

The real macOS service canary passed install/start, idempotent start, SIGKILL
recovery, restart, stop/start, reinstall, uninstall, and config/credential/
dedup preservation. The corrected run passed in 16.25 seconds; a run sending
an event to a real Desktop thread passed in 16.46 seconds. The initial
bootout/bootstrap race is retained as a failure report.

```bash
.venv/bin/python scripts/service-canary.py --run \
  --python /absolute/installed-venv/bin/python --report /tmp/codex-monitor-launchd.json
```

Debian 12 arm64 with Codex CLI 0.153.4 passed nine real TUI checks in 33.481
seconds, covering user input, active contention, drafts, idle behavior,
post-exit resume, and single processing.

## Native Unix WebSocket and packaging

An isolated native Unix-socket protocol probe passed `initialize` and
`thread/loaded/list`, kept the RPC connection open after an empty response,
and exited cleanly. The initial failure was caused by the Python client's
compression offer; disabling compression fixes the handshake. This probe used
temporary state and did not call the model or touch a user thread.

The package passed `pip check` and `pip wheel --no-deps`. The release wheel is
`codex_monitor-0.1.0-py3-none-any.whl`. The 2026-09-08 archive check verified
hashes, relative-wheel installation, status, upgrade, removal, and state
preservation in isolated macOS and Debian 12 arm64 environments. The final
macOS regression was 66 tests in 21.880 seconds, including five installer
tests. Archive validation is not a TUI check and does not publish a registry
release or prove discovery in a new Codex session.

## Remaining verification

Desktop pixel automation, Desktop draft and approval-waiting cases, SSH
remote Desktop projects, Windows/WSL, macOS reboot/sleep, an external public
webhook reverse proxy, and model-quality behavior during long real tasks remain
unverified. Fake or protocol-only results do not replace those checks.

The managed-collector macOS LaunchAgent canary passed ten checks in 8.604 seconds: installation/readiness, silent baseline, changed-file receipts, SIGKILL recovery without duplicates, checkpoint preservation, stop and uninstall cleanup. This used the real installed service and a fake App Server peer; it is not Desktop or model-delivery evidence.

A fresh-skill TUI discovery probe initially timed out at the workspace trust prompt. Its first retry incorrectly matched a success marker in the echoed user prompt before an assistant response; review invalidated that PASS. Discovery checks must establish an actual skill listing or response, never match the input text alone. This does not invalidate the separate managed-event TUI canary, which checks native consumed input and the follow-up agent message.

The corrected discovery check used native TUI autocomplete rather than a model success marker. Typing `$codex-mon` exposed the selectable Codex Monitor skill and its insertion hint. The owned TUI and temporary directory were cleaned. This is a discovery PASS, not evidence of natural-language workflow completion or Desktop discovery.

## Process isolation and condition policy (2026-09-09)

The candidate source suite passed 100 tests on macOS in 24.023 seconds. New coverage includes bounded/per-conversation sampler capacity, lifecycle epochs, finite deadlines, schema migration during concurrent startup, durable debounce, downstream failure replay, observation gaps and candidate status. These deterministic tests do not prove real-client interaction or kernel-level fault recovery.

Use a clean installed wheel for the process canary:

```bash
python3 scripts/managed-monitor-isolation-canary.py --run \
  --python /absolute/installed-venv/bin/python \
  --report /tmp/codex-monitor-isolation.json
```

The fixture blocks an exact watched-file read in a real child process and delays another. It verifies healthy progress in another conversation, parent SIGKILL cleanup and stale-result rejection, then exercises durable debounce. Its App Server peer is fake; queue/history assertions are process-contract evidence only. A fixture whitelist mismatch initially rejected the new conversation IDs and caused a timeout; preserve that failed run separately from corrected results.

For actual ordinary TUI behavior and a timed sampler soak, use `managed-monitor-canary.py --run --python ... --tui --soak-seconds 300`. The report must distinguish the real TUI section from fake-peer process assertions. Earlier one-hour transport tests do not establish a one-hour result for the new process sampler.
