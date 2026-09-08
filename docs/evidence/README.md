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
