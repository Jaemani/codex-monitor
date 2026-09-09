# Public verification summary

[PUBLIC-SUMMARY.json](PUBLIC-SUMMARY.json) records observed results, boolean checks and timings from local tests on 2026-09-08. This is a curated summary, not a raw transcript or independent verification.

Raw reports, terminal buffers, conversation IDs and host paths remain local and are excluded from Git. Report names in the documentation identify those local artifacts. Failures and incomplete runs remain represented; a later PASS does not erase them.

Key observations:

- Dashboard open, 2026-09-09: source native PASS in 25.878 seconds; installed-wheel native PASS
  in 18.634 seconds. Enter opened the selected existing ordinary TUI, one user follow-up and response
  remained in the same native history, and `/quit` returned to the dashboard. Owned thread archival,
  process exit and workspace removal passed. Local reports: `codex-monitor-dashboard-open.json`
  and `codex-monitor-dashboard-open-installed.json`. Final regression: 191 tests in 51.146 seconds;
  installed dashboard PTY: 39 checks in 7.728 seconds. Wheel SHA-256:
  `80f15b1b09c546d301f3f4835793a959b63684302bff62839bf86b519cd92af9`.
  Global upgrade matched all 21 modules, preserved twelve bindings/seven receipts/credentials and
  returned the receiver ready. No operational owner/resident restart or Desktop wakeup was tested.

- Metadata-only resident resume, 2026-09-09: 185 regression tests passed in 51.917 seconds;
  an isolated ordinary-TUI source resident run passed in 72.701 seconds with two tasks,
  reconnect, a 23.0-second owner outage and four events with one native input/response each.
  Fixtures were archived and cleaned up. Local report: `codex-monitor-resident-exclude-turns.json`.
  This confirms native compatibility of the new resume option, not the reported large-history
  failure's cause or recovery, live service deployment, or Desktop ownership.

- Expanded CLI resident run: two ordinary TUI-created tasks, four independent test events, each
  consumed once with one native response. A 23.3-second owner outage retained an event with zero
  spent attempts; transport-only reconnect and owner restart restored availability. Native run:
  75.617 seconds; final regression: 185 tests in 50.880 seconds. Both fixtures were archived before
  final PASS. Prior driver failures and their verified causes remain in the JSON summary.

- CLI resident, 2026-09-09: a bounded one-conversation ordinary TUI run passed in 26.800 seconds.
  It covered event consumption while the TUI was closed, return to the same task, user follow-up and
  owner restart with subscription recovery. The post-review runtime passed 183 regression tests;
  an isolated wheel install passed 51 focused tests. Local upgrade preserved eight bindings,
  credentials and seven receipts. Earlier incomplete/timeout runs remain in the summary. This is
  not a Desktop owner fix or a long-duration resident/approval-screen result.

- Actual ordinary Codex TUI on macOS and Debian arm64, and Unix remote TUI: PASS.
- Actual TUI one-hour soak: 3,600.025 seconds, 12 events, 3 client restart/resume cycles.
- Receiver outage: 3,600.15 seconds, 665 health samples, 55 graceful restarts. An earlier incomplete run remains recorded.
- Desktop: same-conversation native delivery and explicit reply round trip; user-observed app restart and visible event input. Desktop draft and approval contention remain unverified.
- Managed-feature regression: 75 tests PASS on macOS and Linux; 16 installed process checks each; actual macOS TUI event and user follow-up PASS; 60-second managed-collector soak PASS. Managed Desktop event arrival in the same conversation confirmed; no new pixel verification. Final release archive installation, upgrade and uninstall: macOS and Debian arm64 PASS.
- Claude comparison is based on official documentation, not a matched live failure/latency experiment.

See [testing procedures](../TESTING.md), [compatibility](../COMPATIBILITY.md), and [project TODO](../TODO.md) for scope and remaining work. New test runs should stay private until deliberately summarized for publication.

The 2026-09-09 process-sampler/debounce wheel passed 100 tests and 17 installed failure/recovery checks on macOS and Linux, the macOS LaunchAgent lifecycle and archive installer lifecycle. Its implementation commit passed all four hosted CI jobs. A separate real macOS TUI event/follow-up check passed; the sampler phase completed 300.155 seconds with 30 events and no duplicates. The original combined run remains INCOMPLETE because its TUI phase lacked a test dependency. See the curated JSON summary for hashes and scope.

The request/explicit-endpoint wheel passed 124 macOS/Linux tests, 32 installed
Linux request checks and 37 macOS checks including the actual request TUI flow.
The managed Unix remote TUI passed with one sampled 1.032-second change-to-visible
interval. Archive installation/removal preserved request state. A Linux sampler
cleanup failure without container init was reproduced as terminated zombie
children; the same wheel passed all 17 checks with `--init`. The original
combined failure remains recorded separately from its successful phases and
the corrected run. These results do not establish a latency guarantee, new
Desktop screen coverage or completed business work.
