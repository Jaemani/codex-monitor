# Project TODO

Updated: 2026-09-08. The goal is reliable external-event participation in the user's ongoing Codex conversation, with ordinary CLI TUI as the primary interface and Desktop also supported.

This file is the project backlog. Update it with the corresponding code change; record measured results in STATUS.md and the public evidence summary. Do not mark a platform or user flow done from protocol tests alone.

## Target use case

The next integration target is a Discord project PM/relay agent. See [operating levels and pilot gates](DISCORD-PM.md). The delivery foundation is verified; a Discord relay and unattended worker-status monitoring are not yet implemented. Prioritize managed producers and a complete single-project Discord round trip before claiming always-on operation.

## Next: reliable everyday use

- [ ] **P1 — Manage event producers ([#2](https://github.com/Jaemani/codex-monitor/issues/2)).** Start, stop, restart and inspect a file/CI/webhook producer separately from receiver liveness. A configured binding must not imply an active watch. Verify producer crashes and recovery.
- [ ] **P1 — Reduce event latency through supported APIs ([#1](https://github.com/Jaemani/codex-monitor/issues/1)).** Measure ingress → queue acceptance → consumer start → visible response separately. Assess official same-owner push routes for CLI and Desktop. See [technical latency comparison](LATENCY.md). Shared-local currently observes external changes at roughly 10-second intervals; do not claim real-time delivery or use private Desktop IPC.
- [ ] **P1 — Verify skill onboarding in fresh clients ([#3](https://github.com/Jaemani/codex-monitor/issues/3)).** Discover `$codex-monitor` in a new ordinary TUI and Desktop session; select a real producer; start, inspect, pause, resume, reply and stop without changing the target conversation.
- [ ] **P1 — Complete Desktop interaction cases.** Unsent draft, event during active response, approval waiting and cancellation recovery; retain actual user-visible observations.
- [ ] **P1 — Ship a complete Discord adapter for a supervised single-project pilot.** Authenticate the producer, filter relevant changes, preserve stable event IDs, and implement explicit source-scoped replies.

## Compatibility and resilience

- [ ] **P1 — Version compatibility probe and matrix.** Current runtime evidence targets Codex 0.153.4 and experimental queue APIs. Fail clearly on unsupported contracts and rerun real-client canaries after upstream changes.
- [ ] **P1 — Windows/WSL.** Validate runtime transports and ordinary TUI. Current POSIX installer refuses Windows; do not advertise installer parity.
- [ ] **P2 — Desktop remote/SSH.** Test an explicitly configured remote host and exact conversation ownership.
- [ ] **P2 — OS sleep and reboot.** Verify receiver/producer restart, credential preservation and same-conversation event recovery.
- [ ] **P2 — Public webhook deployment.** Validate a selected reverse proxy, authentication, body/rate limits and outage recovery.
- [ ] **P2 — Matched Claude comparison.** Use the same event workloads, restart/failure cases and latency measurements. Document adapter differences; no overall superiority claim before evidence.

## Distribution and maintenance

- [x] **First hosted CI passed.** macOS/Linux × Python 3.11/3.14 regression and packaging: [CI workflow](https://github.com/Jaemani/codex-monitor/actions/workflows/ci.yml). This does not replace real-client UI tests.
- [ ] **P2 — Publish a versioned release.** Rebuild and verify the final archive, document dependencies and checksums, and decide release support policy. A public repository is not a package-registry release.
- [ ] **P2 — Choose a license.** Public source visibility alone does not grant an open-source license.
- [ ] **P2 — Upgrade/rollback usability.** Simplify receiver migration across runtime releases while preserving explicit ownership and state.

## Completed baseline

- [x] Official shared-local delivery into an existing conversation without starting/resuming threads or interrupting turns.
- [x] Actual ordinary TUI user/event/user interaction, draft preservation, idle silence and offline queue consumption.
- [x] macOS/Linux TUI baseline, Unix remote TUI, one-hour TUI soak.
- [x] Desktop same-conversation delivery and user-observed restart/event visibility.
- [x] Durable inbox, deduplication, bounded retries, uncertain-delivery inspection and explicit source-scoped reply outbox.
- [x] Receiver lifecycle, crash/restart and one-hour unavailable-endpoint validation.
- [x] Human-readable events, attach/sessions/pause/unpause/reply and honest unknown producer/target status.
- [x] Runtime installer and skill/plugin assets; isolated macOS/Linux archive lifecycle tests.
- [x] 66 local regression tests and official skill/plugin format validators.

Raw transcripts remain local. See [public evidence](evidence/README.md) and [compatibility](COMPATIBILITY.md).
