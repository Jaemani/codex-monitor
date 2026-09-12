# Test design and results

## Full candidate verification: 2026-09-12

Rust runtime: `c8a1a28`, with the subsequent TUI-driver compatibility/cleanup fix.
The Python checkout included the pending dashboard-readiness correction; its
208-test result must not be attributed to the unchanged published Python source.

| Surface | Result | Scope |
|---|---|---|
| macOS Rust | 29 passed; fmt, strict Clippy and release build passed | Disposable contracts; no model |
| Docker Debian arm64 Rust 1.97.1 | 29 passed; release build passed | Native Linux container, isolated source copy |
| macOS Python 3.14 | Final 211 passed in 55.399 s | Full checkout suite including three new cleanup regressions; initial 208 also passed |
| Docker Debian arm64 Python 3.11 | 208 passed in 59.624 s | Clean dependency installation and copied checkout |
| Matched Python/Rust functional harness | 61 checks each passed | HTTP, predicates, lifecycle, paths and isolation; fake/unavailable owner |
| Ordinary Codex 0.154.0 TUI, Rust release | 19 checks passed in 35.754 s | Visible event, user follow-up, closed TUI, receiver restart, owner restart/resubscribe, same-task reopening, cleanup |
| Python dashboard PTY | 42 display + 22 control checks passed | Monitor UI, not native conversation consumption |
| Rust dashboard PTY | Passed | Queue-only Enter guidance, pause/resume, clean exit |
| Installed Python wheel | 17 isolation + 32 request + 17 predicate checks passed | Child hang/SIGKILL, lifecycle, 60-second predicate soak; no model |
| Packaging/publication | Passed on macOS and Docker Linux | Archive construction and clean wheel installation; no release published |
| TUI cleanup regressions | 3 passed | Exited-child handling, signal fallback and descriptor cleanup |

The first Rust TUI attempt failed on remote resume: Codex 0.154.0 rejects
permission overrides when resuming an existing remote task. Reopening now keeps
its existing permissions. The old cleanup path also raised `PermissionError`
while signaling an exited child and prevented the final report; it now reaps
before signaling, closes the descriptor reliably and runs independent cleanup
steps before saving a failure. The failed run was retained, its owned processes
terminated and its disposable task archived. The corrected run passed.

Three installed-wheel canaries initially rejected an editable checkout as
intended. They were rerun successfully with a clean environment containing the
new archive's wheel. This environment correction is not a product defect.

These results do not establish Desktop cold-task wakeup, Windows/WSL, SSH,
OS reboot/sleep, Rust installer/migration parity or a new one-hour outage result.
The 60-second predicate soak is Python evidence. Raw reports remain outside Git.


## Dashboard groups and monitor controls

```bash
.venv/bin/python scripts/dashboard-controls-canary.py --run \
  --report /tmp/codex-monitor-dashboard-controls.json
```

This disposable PTY canary creates two projects with the same conversation label and two routes
in one conversation. It verifies exact-route stop/resume, named delete confirmation, cancellation
by another key, retirement and receipt preservation. It starts no native conversation or model.
Use `--dashboard-python /absolute/installed-runtime/bin/python` for an installed UI. Unit coverage
also checks duplicate-name suffixes, stale identity rejection, managed lifecycle epochs and retired
route intake/dispatch guards. Keep these controls checks separate from ordinary-TUI interaction.


## Dashboard to ordinary Codex TUI

The open action must preserve the selected conversation ID and explicit owner endpoint, pass
credentials through the environment, reject changed or unsafe bindings, and restore the terminal
after child exit or failure. Snapshot refreshes must never launch Codex. Validate separately from
the existing read-only dashboard PTY suite:

```bash
.venv/bin/python scripts/dashboard-open-canary.py --run \
  --report /tmp/codex-monitor-dashboard-open.json
```

The canary requires the development `pyte` extra and authenticated Codex 0.153.4. It uses Luna/xhigh
on one disposable Unix-owner conversation with both shared-local and explicit owner routes. It
checks the preferred route, cycles routes with Tab, verifies safe rejection of shared-local opening,
then opens the explicit route and verifies a user follow-up in that same native history. It exits
the TUI with `/quit` and checks dashboard return. It archives
the fixture and checks process/workspace cleanup before PASS. Pass `--dashboard-python` with an
absolute installed Python path to exercise an installed dashboard instead of the source checkout.
This is user-interaction evidence, not a new external-event, Desktop or production-service test.

## CLI resident lifecycle

Unit tests cover explicit registration, incompatible queue targets, isolated per-task failures,
connection loss, bounded reconnect backoff, cancellation and no queue writes or approval responses.
CLI tests cover exact-thread TUI reconnect and foreground shutdown without creating receiver state.
These tests do not establish native conversation consumption.

The metadata-only resume patch passed all 185 regression tests in 51.917 seconds. Its isolated
ordinary-TUI resident rerun passed in 72.701 seconds, including two closed-TUI conversations,
transport reconnect, a 23.0-second owner outage and four events each consumed/responded to once.
This exercises native `excludeTurns` compatibility, not a large-history frame-limit reproduction
or recovery of the reported deployment. The harness uses the source `ResidentKeeper` API.

The opt-in real-client canary uses an owned Unix App Server and ordinary Codex TUI:

```bash
.venv/bin/python scripts/resident-canary.py --run --model gpt-5.6-luna \
  --report /tmp/codex-monitor-resident.json
```

It requires the development `pyte` extra and authenticated Codex 0.153.4, and uses real model tokens
at `xhigh`. Keep raw reports and terminal buffers local. Scope every reported result to the cases
actually completed; disconnected-TUI processing does not establish Desktop wakeup, unattended
approval handling, OS reboot recovery or long-duration residency.

The expanded run creates two ordinary TUI tasks, retains both subscriptions with both TUIs closed,
checks independent exact client IDs and responses, reopens one task for user input, disconnects
the resident without restarting the owner, then holds an owner-down event beyond the former retry
budget window. After owner restart it checks both loaded targets, backlog consumption and a new
event. The driver waits for native `completed` turns before closing a TUI; an interrupted turn may
legitimately hold later input. Native state and rendered composer input are separate observations.
Final PASS is written only after fixture archival and process/workspace cleanup. See the evidence
summary for earlier driver failures and measured results.

## Queue compatibility incident checks (2026-09-09)

The full 162-test suite passed. Doctor tests cover `-32601`, the exact 0.147.0 `-32600` unknown-method
variant, unrelated invalid requests, and shared-local success with unknown consumer readiness.
An isolated official 0.147.0 binary reproduced the missing API without loading a conversation or
starting a model turn. The CLI receipt test verifies queued/consumed/unknown diagnostics without
mutations or new RPC methods. The actual Desktop incident remains open: one original consumption
and a substantive response have not been verified. Upstream cold-thread tests were read, not run here.

## Graphical dashboard and global command verification (2026-09-09)

The graphical dashboard adds a persistent live header, color-coded status labels and selectable
conversation-grouped rows. Verify animation separately from snapshot reads: a pulsing indicator is
not producer health. Check plain snapshots, color overrides, `NO_COLOR`, reduced motion, selection
in long inventories, details, narrow/CJK clipping and terminal restoration. PTY reports below cover
this separate terminal view, not native Codex conversation delivery.

The installer command link must point to the stable launcher and pass arbitrary CLI flags from
another working directory. Verify an existing runtime can register its link without a receiver
restart; custom-prefix tests must stay inside their temporary directory. Exercise upgrade continuity,
foreign command conflicts, modified-link preservation, legacy ownership markers and uninstall.
Record actual candidate and installed results in STATUS.md and the public evidence summary.

## Read-only dashboard and OS health-hook example (2026-09-09)

The dashboard regression covers no runtime/RPC construction, read-only inventory, exact filtering,
missing/corrupt state, terminal-control sanitization, narrow rendering, full snapshots and authenticated
receiver readiness including failure responses. The health-hook regression uses an HTTP fixture and
the durable inbox for quiet healthy checks, consecutive failures, lost-ack deduplication, quiet
recovery and a distinct later outage. A separate real probe subprocess sends through the CLI to an
authenticated receiver with a local sink. These tests do not invoke a Codex model or repair a server.

The opt-in dashboard canary drives an actual PTY and checks navigation, resizing, receiver
outage/recovery, missing-state preservation and terminal settings after `q` and Ctrl-C. It compares
database semantics before and after observation. Use an installed Python path to test a wheel
independently from the checkout:

```bash
.venv/bin/python scripts/dashboard-canary.py --run \
  --dashboard-python /absolute/installed-runtime/bin/python \
  --report /tmp/codex-monitor-dashboard.json
```

Requires a POSIX terminal environment and the development `pyte` extra. This validates the dashboard's
own UI, not Codex TUI conversation delivery or Desktop pixels. See STATUS.md for measured runs.

## Earlier delivery evidence

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

## Natural-language installed-skill workflow: PASS

The audited canary `scripts/skill-workflow-canary.py` used the installed
skill helper and installed runtime through an ordinary Codex CLI TUI in an
owned pyte-backed PTY. It preserved the authenticated global `CODEX_HOME`,
while the workspace, monitor state, watched file, receiver and test thread
were disposable. The TUI used `workspace-write` and `on-request`; trust was
limited to the disposable workspace, and command approvals were never
answered automatically.

The canary discovered the exact thread through App Server RPC, then submitted
five natural-language `$codex-monitor` turns: create the managed monitor,
inspect status, pause, resume and remove. Independent installed CLI/HTTP
checks verified the exact binding and receiver. Native history checks used the
delivery's stable client ID, while `inspect` supplied read-only local
`accepted` versus native `consumed` evidence. The active and resumed changes
were consumed exactly once; paused and removed changes created no new receipt
or history entry. The successful run took 323.225 seconds.

```bash
.venv/bin/python scripts/skill-workflow-canary.py --run \
  --report /tmp/codex-monitor-skill-workflow-canary.json \
  --monitor-bin /absolute/installed-runtime/bin/codex-monitor \
  --skill-helper /absolute/installed-skill/scripts/monitor.py \
  --timeout 180
```

The final report and all earlier reports/captures are preserved in
[`docs/evidence/skill-workflow-2026-09-09/`](evidence/skill-workflow-2026-09-09/).
Earlier attempts exposed harness issues involving PTY pumping, macOS
`/tmp` path spelling, receiver credentials and persisted port selection; they
are retained with brief reasons in `RUNS.md`. The PASS is scoped to the
installed basic managed-file skill workflow. It does not verify model task
success, Desktop skill discovery, predicate policies, request lifecycles or
the final candidate source/runtime. The native evidence proves queue/history
consumption and the TUI capture, not business-work completion.

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

The final 300.155-second sampler phase completed with 30 events, one receiver restart and no duplicate hashes. The combined run is INCOMPLETE because the TUI phase lacked the optional `pyte` dependency. Keep this distinction even after a separate TUI rerun; do not rewrite the original combined report as a UI PASS.

The separate final ordinary macOS TUI run passed in 29.954 seconds with the same wheel: managed event display, native consumption and a subsequent user response in native history. It used Luna at xhigh with the test driver dependency installed. This is not a completed business-workflow claim, and no new Desktop pixel result is established.

## Request lifecycle and managed remote CLI (2026-09-09)

### JSON predicate extension

The final predicate runtime passed 134 source tests on macOS and 134 installed
tests on Debian 12 arm64/Python 3.11. Installed predicate process checks cover
silent baselines, sustained conditions, unrelated writes, recovery, invalid
samples, restart/pause timing reset, conversation scope and value redaction.
The stability soak ran for 300.001 seconds with 30 unrelated writes and no
additional matched events. This is a five-minute predicate result, not a new
one-hour result.

```bash
python3 scripts/managed-predicate-canary.py --run \
  --python /absolute/installed-venv/bin/python --soak-seconds 300 \
  --tui --remote --model gpt-5.6-luna --reasoning-effort xhigh \
  --report /tmp/managed-predicate.json
```

The separate stricter ordinary Unix remote TUI run passed in 55.088 seconds.
Matched and recovered events rendered with redacted values, their exact
client IDs appeared once each in native history, and a later user turn
received a response. It rejects command approvals and identifies only its
disposable workspace trust prompt. Earlier broad prompt matching and
state-text-only assertions were replaced; preserved older runs are not the
strict final TUI evidence. Overall report completion now waits for all
requested phases and cleanup.

### Request and endpoint results

The request/explicit-endpoint candidate passed 124 macOS source tests and 124
Linux installed-wheel tests. Request process/HTTP coverage passed 32 checks on
Linux. The macOS request canary passed 37 checks including an actual ordinary
TUI: quiet acknowledgement, retained composer draft, progress consumption
without implicit completion, explicit completed event exactly once, and a
subsequent native assistant response.

```bash
python3 scripts/request-lifecycle-canary.py --run \
  --python /absolute/installed-venv/bin/python --tui \
  --report /tmp/request-lifecycle.json
python3 scripts/managed-monitor-canary.py --run \
  --python /absolute/installed-venv/bin/python --tui --remote \
  --report /tmp/managed-remote.json
```

The driver needs optional `pyte` and an authenticated Codex CLI for TUI checks.
The second command uses a test-owned Unix App Server and ordinary `codex
--remote`. Its recorded run passed 19 checks in 32.474 seconds. It verifies
the configured endpoint and exact loaded thread, native event consumption,
rendered content and a follow-up response. Latency timestamps are sampled
observation times, not precise server-side execution-start timestamps.

For a bounded latency distribution on an owned Unix App Server and ordinary
remote TUI, use the separate opt-in canary after reviewing its model and
approval settings:

```bash
python3 scripts/latency-canary.py --run \
  --python /absolute/installed-venv/bin/python \
  --samples 6 --model gpt-5.6-luna --reasoning-effort xhigh \
  --report /tmp/codex-monitor-latency.json
```

It records producer/file change, local checkpoint intake, HTTP/native
acceptance, exact native history consumption and visible PTY rendering for
idle, busy and post-restart samples. The report includes observed p50/p90/p95
values where the category has enough observations, plus a small-sample caveat.
These are signed observer intervals and bounded observations, not latency
guarantees or service-level objectives. The canary rejects command approval
prompts and only accepts the exact disposable workspace trust dialog.

The final predicate wheel also passed `managed-predicate-canary.py --tui`
without `--remote` in 55.757 seconds, using the ordinary shared-local CLI.
Matched and recovered events rendered and were correlated once each by exact
client ID; the subsequent user response passed. No command approval occurred.

## Consumer readiness gate (2026-09-09)

Fifteen doctor/CLI tests passed. A native check passed four assertions in 0.917 seconds: an isolated
loaded owner passes the strict gate, the current Desktop shared queue keeps default compatibility
behavior, unknown consumption fails the strict gate, and the fixture has no queued input or user turns.
Two initial harness runs assumed a newly started thread already had a persisted rollout/turn list;
those assumptions were corrected and the failure reports retained locally. No Desktop UI interaction
or cold-task wakeup is claimed. The running receiver was not upgraded or restarted for this change.

## Dashboard scope clarification (2026-09-10)

The project virtual environment passed 22 dashboard/control tests in 2.780 seconds. The final
read-only PTY canary passed 42 checks in 6.386 seconds, including narrow resize, route navigation,
receiver outage/recovery, terminal restoration and no model/session calls. An auxiliary full-suite
attempt under system Python encountered three missing-websockets errors; it is not a full-suite PASS.
The change adds scope metadata and names delivery/work reports explicitly. No model/tool telemetry,
new native delivery, Desktop validation or installed-runtime upgrade is claimed.

## Rust candidate checks

The `codex/rust-runtime` branch has a separate executable and state directory.
Python evidence does not count as Rust acceptance. See [adoption gates](RUST-EVALUATION.md).

```sh
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --locked --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path rust/Cargo.toml
```

The contract tests use disposable state and fake native peers; they do not invoke
models. The resource harness requires POSIX `ps`/`wait4`, Python 3.11+ and the
Python runtime dependencies. It samples the receiver process tree, validates each
initial checkpoint and keeps failed cases separate. Use reports outside Git:

```sh
python3 rust/bench/compare_collectors.py \
  --python /path/to/python-environment/bin/python \
  --rust rust/target/release/codex-monitor-rs \
  --counts 0,10,50,128 --duration 30 --interval 10 \
  --output /tmp/monitor-resource-report.json
```

`--duration` applies separately to idle and changed-file phases. A low event count
is a failure, not evidence of efficiency. RSS sums can double-count shared pages
and sampled peaks can miss short-lived workers. Waited CPU covers the whole
receiver lifetime and its reaped descendants. Database/WAL size is not bytes written.

The opt-in TUI check also requires `pyte` and a compatible authenticated Codex CLI.
It creates an ordinary TUI in its own PTY, verifies exact client IDs and visible
history, accepts only its disposable workspace trust prompt, and archives its task:

```sh
python3 rust/bench/tui_canary.py --run \
  --report /tmp/monitor-rust-tui-report.json
```

This is a source release-build check, not installed-package, Desktop, Windows or
long-duration evidence. The existing Python packaging workflow remains separate.

## Recovery follow-up (2026-09-12)

The Python health/rebind candidate passed 222 checkout tests in 50.446 seconds,
42 dashboard PTY checks and 22 dashboard-control checks. The built wheel was
installed through the versioned upgrade path; authenticated receiver readiness
and six existing explicit-owner CLI target observations passed after service
startup. This rollout did not send canary turns to operational conversations.
Account presence is not credential validation or a model-work result.

The Rust fake-owner outage driver (`rust/bench/outage_canary.py`) passed six
checks with a 120.002-second owner outage and a receiver crash/restart during
that outage. The persisted event retained zero submission attempts until owner
recovery, then produced exactly one queue addition. Duplicate ingress and a
further receiver restart did not replay it. No model methods were called.
This is bounded protocol/process evidence, not a one-hour or real-client soak.
Raw reports and operational backups remain outside Git.

The rebuilt Rust recovery candidate passed 34 tests and strict Clippy/formatting,
then 19 ordinary Codex 0.154.0 TUI checks in 34.585 seconds. An isolated macOS
launchd lifecycle initially failed receiver readiness after restart. Replacing
asynchronous bootout/bootstrap restart with `kickstart -k`, and awaiting unload
on stop/remove, fixed the reproduced failure. All nine real service checks and
six mocked installer/service tests then passed. The failed report is retained.
These checks do not establish login/reboot autoload or Linux supervision.

The final rebuilt release repeated the same six outage checks successfully:
120.001 seconds unavailable, 123.951 seconds total, with no start/resume/turn
or interrupt methods and exactly one recovered submission.

Final source verification passed 36 Rust tests after adding actual fake-owner
CLI/RPC coverage and conversation-dot aggregation. Authentication-required,
ready, disconnected and shared-local observations are exercised without
start/resume/model calls. A mixed set of active routes cannot hide a failed or
unverified route behind a healthy sibling. Strict Clippy and formatting pass.
The ordinary TUI run above preceded this final display-only aggregation change.

## Route inspector usability (2026-09-13)

Python dashboard regressions passed 24 tests, including a 34-route inventory,
last-route selection, long-path wrapping and bounded short-terminal scrolling.
The real dashboard PTY passed 46 checks, including the route inspector,
scrolling to TUI availability, return to overview and removal of prior open
notices on navigation. The control PTY passed 22 checks. The built wheel was
installed and matched the source dashboard hash; receiver readiness passed.
No operational routes were paused, deleted or replayed by these UI changes.
Auto-refresh is now static; its test asserts it does not impersonate a health pulse.

The matching Rust UI update passed 38 full Rust tests (11 binary/UI tests),
strict Clippy and formatting. Its static refresh label is separate from receiver
health; active/paused counts and a five-route inspection window are covered.
This adds source UI coverage, not a new real Codex TUI acceptance claim.
