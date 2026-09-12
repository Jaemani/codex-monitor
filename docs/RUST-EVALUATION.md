# Rust runtime evaluation

Date: 2026-09-10. Branch: `codex/rust-runtime`. Tracking:
[#14](https://github.com/Jaemani/codex-monitor/issues/14).

**Decision: keep the Rust implementation as an isolated candidate. Do not switch
the installed Python runtime yet.** Lower resource use is useful evidence, but it
does not close missing workflow or platform coverage.

## Follow-up verification: 2026-09-12

Twenty-nine Rust checks pass; formatting and Clippy with warnings denied pass.
The Rust CLI now supports request inspection with transition history and durable
notifications. Local tests cover explicit/implicit revision replay, automatic
expiry across receiver restart, paused-route isolation, per-request ordering,
removal, expired delivery, rate-limit deferral and idle maintenance without DB
writes. Status notifications use the existing inbox; maintenance does not call a
model. A subsequent source-authenticated HTTP implementation adds scoped
create/list/inspect/update, direct indexed lookups and bounded keyset pagination.

A live pseudo-terminal check exercised dashboard rendering, queue-only Enter
without launching Codex, pause/resume and clean exit. Owner-address routes retain
Enter open; refreshed route identity is preserved, and a replaced selection must
be explicitly selected again before an action. This is monitor UI validation,
not a new real Codex conversation acceptance run.

The macOS long-interval symlink test initially failed repeatedly. Registering the
literal symlink parent could miss subsequent native events even though sampling
resolved its path correctly. The collector now registers the worker-resolved
parent too, shares registration counts, releases aliases on pause/removal and
performs a catch-up sample after registration. Two consecutive changes pass
before the 30-second fallback. No new comparative memory result is claimed for
this revision; earlier measurements below retain their original scope.

## Resource recheck: 2026-09-12

The frozen `c8a1a28` release executable passed matched 0/10-watch observations
with a 2-second fallback interval and 10-second idle/change phases. At 10 watches,
Python/Rust median idle process-tree RSS was 53,216/41,616 KiB (21.8% lower for
Rust); changed-phase sampled peak was 103,440/42,448 KiB (59.0% lower). Both
produced exactly 10 events and 10 changed checkpoints. Rust idle DB/WAL sizes
stayed unchanged and no idle event was generated. Whole-run waited CPU was
8.055/0.091 seconds. Zero-watch Rust median idle RSS was 10,368 KiB.

These are short, host-specific observations after build/test processes finished,
not long-duration guarantees, precise physical memory totals or full parity.
The follow-up ordinary Codex 0.154.0 TUI passed after a test-driver resume fix;
see the dated [full test matrix](TESTING.md#full-candidate-verification-2026-09-12).
Request HTTP is now implemented. Operational-health and distribution validation
are progressing; deliberate state migration remains open.

## What changed structurally

| Area | Python baseline | Rust candidate |
|---|---|---|
| File sampling | New isolated process for each scheduled sample | Reusable workers, at most four concurrently |
| Change detection | Interval sampling | OS notifications plus the configured fallback interval |
| Idle collector state | Periodic per-watch status updates | No per-watch idle heartbeat writes |
| File failure isolation | Child timeout/termination | Child timeout/termination and replacement |
| Scheduling | Bounded sampling capacity | Independently completed tasks with fair scheduling |
| Worker runtime | Python process initialization per sample | Minimal synchronous worker; no async runtime per child |
| Checkpoint/event durability | Durable checkpoint replay into inbox | One SQLite transaction for checkpoint and inbox |
| Native transport | Endpoint connection pool | Bounded endpoint pool with cancellation poisoning |
| HTTP resources | Bounded Python server | Bounded socket admission, headers, handlers and body size |
| Ownership | Existing Codex queue/explicit resident owner | Same ownership model; no new Desktop wake mechanism |

The sample pool reuses idle workers; a single watch does not gradually create
four worker processes. File read buffers are 64 KiB. Slow samples no longer hold
an entire batch of unrelated file observations. Native notifications are filtered
to changes and use a bounded queue with fallback resampling after overflow.

## Verification boundaries

Twenty Rust contract checks passed, including durable ingress/restart,
source/admin authentication, conflict replay, watch epochs/checkpoints, retry
backoff, stale completion, Unix WebSocket handshake, paginated native inspection
and native connection reuse. Formatting and Clippy with warnings denied passed.
These are local contract checks, not proof of all deployed environments.
Hosted Rust checks subsequently passed on both macOS and Ubuntu. The Python
matrix initially passed three entries; macOS/Python 3.14 exposed a resident test
that could miss a 10 ms failure window and leave its worker alive after failure.
The test now holds the simulated outage until observed, verifies recovery and
always stops the worker. This changes test synchronization, not runtime behavior.
A subsequent long-interval test found that macOS reported `/private/tmp` for a
watch registered under `/tmp`. The collector now obtains its native path key
inside the killable worker, including arbitrary parent-directory aliases, while
preserving the user's original path in public events. A real subprocess test
requires notification within four seconds with a 30-second fallback interval.
Canonicalization never blocks the receiver reactor.

A real ordinary Codex 0.153.4 TUI was created in a disposable PTY with an owned
Unix App Server. The final source release build passed initial user response,
visible external event, exact native client ID, duplicate suppression, user
follow-up, resident delivery after closing the TUI, receiver restart and reopening
that same conversation. An extended run also stopped and restarted the owner App Server, verified
resident resubscription, consumed the retained offline event exactly once and
rendered it after reopening the same TUI. That run completed in 46.779 seconds
and cleaned up its owned task and state. An earlier failed real run found a missing Unix WebSocket
handshake header despite passing mocked tests; that defect was fixed and given a
regression test. The failure remains part of the local evidence.

Raw transcripts and benchmark reports stay outside Git. The reusable drivers are
in [`rust/bench`](../rust/bench). They do not operate on the installed service or
operational conversations.

## Resource methodology

The matched resource driver creates equivalent 4,077-byte files and at most
32 watches per synthetic conversation. It requires every watch to persist the
initial digest before starting the idle phase, then writes and holds one changed
version. Both runtimes must persist exactly one event per watch and exit cleanly.
The endpoint is deliberately unavailable, so native/model work is excluded.

Each case measures idle and changed-file phases separately. Reported RSS sums
the receiver and all sampled descendants. This double-counts shared pages, and
200 ms sampling can miss short-lived Python workers. Whole-run CPU comes from
POSIX `wait4` and includes descendants reaped by the receiver; it includes startup,
both phases and shutdown. DB/WAL allocated sizes are not total bytes written.

Early reports with port collisions or incomplete initial checkpoints are invalid
comparisons and are excluded. In particular, a change made before the first sample
cannot be counted as a lost transition. Any overloaded case that cannot establish
all initial checkpoints is reported separately from valid memory comparisons.

## Measured local resources

macOS, one shared development host; one 30-second idle phase and one 30-second
changed-file phase per case, 10-second fallback interval. These are observations,
not certified ceilings or isolated-host benchmarks. MiB values below divide
sampled KiB by 1,024.

| Watches | Python idle median | Rust idle median | Python changed-file peak | Rust changed-file peak | Stable events |
|---:|---:|---:|---:|---:|---|
| 0 | 33.47 MiB | 10.03 MiB | 33.52 MiB | 10.05 MiB | Both 0 |
| 10 | 52.33 MiB | 39.59 MiB | 102.47 MiB | 40.66 MiB | Both 10/10 |
| 50 | Not comparable | 40.36 MiB | Not comparable | 41.42 MiB | Rust 50/50 |
| 128 | Not comparable | 41.70 MiB | Not comparable | 42.80 MiB | Rust 128/128 |

At 10 watches, Rust used 24.3% less median idle RSS and 60.3% less observed peak
RSS during the changed-file phase. Whole-run waited CPU was 0.096 seconds for
Rust and 8.561 seconds for Python. The Python tree spawned short-lived samplers;
Rust reused its four workers and created no additional sampled processes during
the changed-file phase. Do not interpret the CPU ratio as model-token savings.

The 50/128-watch Python cases could not establish all initial checkpoints within
90 seconds (36/50 and 72/128 respectively). They were rejected before memory
comparison. This is a workload initialization limitation observed in these runs,
not proof of permanent event loss or a general throughput ceiling. The Rust
cases completed, but no Python/Rust memory ratio is reported for them.

A separate corrected functional driver passed 61 assertions per runtime and four
normalized comparisons. It verifies per-binding baselines/transitions, predicate
envelopes, debounce, pause/resume, invalid JSON recovery, receiver restart,
symlink/FIFO observation and recovery, and actual cross-conversation isolation.
An HTTP paused-target status difference discovered by that driver was fixed and
covered by a receiver-process regression. Diagnostic detail and route-removal
interfaces still differ, as recorded by the driver; full workflow parity is not
claimed. The unchanged Python source suite also passed all 206 tests.

A second frozen-build run used a two-second fallback interval and separate
20-second phases. Rust passed 128 changed checkpoints with exactly one persisted
event on each of 128 bindings. The 10-watch pair passed on both runtimes: idle
median RSS was 40.03 MiB for Rust versus 52.20 MiB for Python; changed-file peaks
were 40.88 MiB versus 96.94 MiB. The overloaded Python 128-watch case again failed
the initial-checkpoint gate (69/128 within 30 seconds) and was not compared.

After the generic native-path fix, the final rebuilt binary passed another
10-watch comparison at a two-second fallback interval with 20-second phases.
Both runtimes stored exactly one event per binding and all ten changed checkpoints.
Rust idle median RSS was 41,584 KiB versus Python's 53,392 KiB (22.1% lower);
changed-file peak was 42,560 KiB versus 100,448 KiB (57.6% lower). Whole-run waited
CPU was 0.134 versus 13.189 seconds. This confirms useful improvement after the
correctness fix rather than relying only on the earlier, smaller Rust build.

These results support continuing the Rust approach for managed collectors. They
do not close the default-adoption gates below.

## Remaining adoption gates

- Final-candidate request workflow acceptance must complement the now-passing
  notification, expiry, history and HTTP contracts.
- Operator resolution of unresolved native handoffs and per-watch error/freshness
  reporting need parity; an ambiguous handoff is retained rather than replayed.
- An isolated versioned binary installer is implemented. Service lifecycle and
  cross-version upgrade/rollback acceptance remain separate from deliberate
  Python-to-Rust state migration. The default runtime remains Python.
- The Rust dashboard needs full visual/controls acceptance against the existing
  Python dashboard, including narrow terminals and duplicate display names.
- Linux/Windows/WSL, SSH, long outages and long-duration Rust runs need their own
  evidence. Prior Python passes do not transfer across the rewrite.
- The Desktop unloaded-task limitation remains unchanged. See
  [ownership limits](OWNER-LIFECYCLE.md).

A default switch requires closing these gates in addition to repeatable resource
improvement. A build succeeding or a single lower RSS number is insufficient.
