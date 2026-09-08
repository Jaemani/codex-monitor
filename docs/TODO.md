# Project TODO

Updated: 2026-09-09. The goal is reliable external-event participation in the user's ongoing Codex conversation, with ordinary CLI TUI as the primary interface and Desktop also supported.

This file is the project backlog. Update it with the corresponding code change; record measured results in STATUS.md and the public evidence summary. Do not mark a platform or user flow done from protocol tests alone.

## Technical progression

Prioritize [conversation-scoped monitoring levels](MONITOR-LEVELS.md). Local file collectors now run per conversation with checkpoint recovery. Next come broader failure isolation, condition policies and request tracking. Discord is a later integration example, not the core milestone.

## Next: reliable everyday use

- [x] **Conversation-scoped local file monitors.** Create/list/status/pause/resume/remove use the exact conversation. Identical names in two conversations, persistent checkpoints, receiver-owned sampling, installed process isolation and actual ordinary TUI event/follow-up checks passed. The managed Desktop event also arrived in the same conversation after the preceding assistant turn ended; pixel and approval cases remain unverified.


- [ ] **P1 — Manage event producers ([#2](https://github.com/Jaemani/codex-monitor/issues/2)).** Managed local files now expose observed collector state and checkpoint recovery under the receiver. Bounded process isolation passed installed failure/recovery checks. Remaining: broader CI/webhook producers, longer endurance coverage and OS sleep/reboot. A configured binding must not imply an active watch.
- [ ] **P1 — Conditional monitor policies (level 4).** Durable per-monitor stable-sample debounce passed installed failure/recovery checks. General condition predicates and escalation policies remain future work. Verify restart during a pending condition, recovery before threshold, rapid oscillation, pause/resume and independent conversations. Keep evaluation outside the model.
- [x] **Correlated request lifecycle foundation (level 5).** Durable request identity, explicit state transitions, expiry, ordered notification outbox and scoped CLI/HTTP passed installed macOS/Linux and ordinary TUI checks. Delivery acceptance never implies completed work. See [request workflow](REQUEST-LIFECYCLE.md).
- [ ] **P2 — Request retention and archival.** Provide explicit bounded archival with a documented deduplication horizon; preserve unresolved native delivery evidence. Current records are retained up to the store capacity.
- [ ] **P1 — Reduce event latency through supported APIs ([#1](https://github.com/Jaemani/codex-monitor/issues/1)).** Managed monitors now accept explicit owner endpoints. Actual Unix `codex --remote` TUI passed with one sampled change-to-visible interval of 1.032 seconds; this is not a latency guarantee. Broader latency distributions and Desktop owner paths remain. See [technical latency comparison](LATENCY.md). Shared-local currently observes external changes at roughly 10-second intervals; do not claim universal real-time delivery or use private Desktop IPC.
- [ ] **P1 — Verify skill onboarding in fresh clients ([#3](https://github.com/Jaemani/codex-monitor/issues/3)).** Fresh ordinary TUI native skill autocomplete passed. Remaining: fresh Desktop discovery and complete natural-language operation; select a real producer; start, inspect, pause, resume, reply and stop without changing the target conversation.
- [ ] **P1 — Complete Desktop interaction cases.** Unsent draft, event during active response, approval waiting and cancellation recovery; retain actual user-visible observations.
- [ ] **P2 — Add an external adapter after conversation-scoped collector lifecycle is verified.** Authenticate the producer, filter relevant changes, preserve stable event IDs, and implement explicit source-scoped replies.

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
- [x] 100 macOS/Linux regression tests and official skill/plugin format validators.

Raw transcripts remain local. See [public evidence](evidence/README.md) and [compatibility](COMPATIBILITY.md).
