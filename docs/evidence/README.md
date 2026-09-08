# Public verification summary

[PUBLIC-SUMMARY.json](PUBLIC-SUMMARY.json) records observed results, boolean checks and timings from local tests on 2026-09-08. This is a curated summary, not a raw transcript or independent verification.

Raw reports, terminal buffers, conversation IDs and host paths remain local and are excluded from Git. Report names in the documentation identify those local artifacts. Failures and incomplete runs remain represented; a later PASS does not erase them.

Key observations:

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
