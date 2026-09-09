# Project TODO

Updated: 2026-09-09. The goal is reliable external-event participation in the user's ongoing Codex conversation, with ordinary CLI TUI as the primary interface and Desktop also supported.

This file is the project backlog. Update it with the corresponding code change; record measured results in STATUS.md and the public evidence summary. Do not mark a platform or user flow done from protocol tests alone.

## Technical progression

Prioritize [conversation-scoped monitoring levels](MONITOR-LEVELS.md). Local file collectors now run per conversation with checkpoint recovery. Next come broader failure isolation, condition policies and request tracking. Discord is a later integration example, not the core milestone.

## Next: reliable everyday use

- [x] **Global command registration.** The default runtime installer owns a stable
  `~/.local/bin/codex-monitor` link; `link` registers an existing install without restarting it.
  Custom prefixes require an explicit command directory. Foreign commands and modified owned links
  are preserved; shell configuration is unchanged. Plugin-only installation still requires runtime setup.
- [x] **Graphical connection overview.** Conversation-grouped rows, selected details, green/red/amber
  status labels and a persistent animated live header replace repeated diagnostics in the main view.
  Color and animation can be disabled. Live refresh is distinct from producer/model health; monitor
  grouping does not invent agent ancestry. See [dashboard guide](DASHBOARD.md).

- [x] **Read-only multi-conversation terminal dashboard.** `dashboard` provides a live summary and
  scrollable details, plus text/JSON snapshots and conversation filtering. It separates receiver
  readiness, delivery configuration, collector observations and explicit request states. It uses
  bounded read-only queries without model calls. See [dashboard scope](DASHBOARD.md) and the dated
  verification in STATUS.md. Native badges, unregistered agents and cross-host discovery remain
  outside this initial implementation.
- [x] **OS timer health-hook example.** A small HTTP probe emits only after consecutive failed checks,
  persists outage IDs across failed sends, and stays quiet after acknowledgement and on recovery.
  Local HTTP/inbox and real probe-process/receiver checks passed. Actual timer deployment and
  model-driven recovery require a selected service and authorized procedure; see [recipe](HEALTH-HOOK.md).

- [x] **Event-first session guidance.** Explain optional relay conversations, full-envelope forwarding,
  concise destination replies and separate configuration/process/delivery evidence. Updated local skill
  installed; this does not establish fresh-client behavioral validation.
- [ ] **External producer health integration.** Surface observed connection state and freshness from
  authenticated producers. Receiver liveness, REST credentials and an old successful receipt must not
  imply a currently connected event stream. Preserve unknown until observation exists.

- [x] **Conversation-scoped local file monitors.** Create/list/status/pause/resume/remove use the exact conversation. Identical names in two conversations, persistent checkpoints, receiver-owned sampling, installed process isolation and actual ordinary TUI event/follow-up checks passed. The managed Desktop event also arrived in the same conversation after the preceding assistant turn ended; pixel and approval cases remain unverified.


- [ ] **P1 — Manage event producers ([#2](https://github.com/Jaemani/codex-monitor/issues/2)).** Managed local files now expose observed collector state and checkpoint recovery under the receiver. Bounded process isolation passed installed failure/recovery checks. Remaining: broader CI/webhook producers, longer endurance coverage and OS sleep/reboot. A configured binding must not imply an active watch.
- [ ] **P1 — Conditional monitor policies (level 4).** Durable debounce and bounded JSON predicates passed installed macOS/Linux process checks, including invalid samples, restart/pause timing reset and independent conversations. A 300-second unchanged-condition soak passed. Actual ordinary default shared-local and Unix remote TUI matched/recovery events and user follow-up passed with exact client-ID correlation; escalation policies remain future work. Keep evaluation outside the model.
- [x] **Correlated request lifecycle foundation (level 5).** Durable request identity, explicit state transitions, expiry, ordered notification outbox and scoped CLI/HTTP passed installed macOS/Linux and ordinary TUI checks. Delivery acceptance never implies completed work. See [request workflow](REQUEST-LIFECYCLE.md).
- [ ] **P2 — Request retention and archival.** Provide explicit bounded archival with a documented deduplication horizon; preserve unresolved native delivery evidence. Current records are retained up to the store capacity.
- [ ] **P1 — Reduce event latency through supported APIs ([#1](https://github.com/Jaemani/codex-monitor/issues/1)).** Managed monitors now accept explicit owner endpoints. A six-sample actual Unix `codex --remote` TUI run passed: idle 0.793–1.150 seconds, post-receiver-restart 0.585–0.633 seconds, busy generation 30.005–33.051 seconds. Two samples per scenario are not a dependable tail estimate. Broader workloads and Desktop owner paths remain. See [technical latency comparison](LATENCY.md). Shared-local currently observes external changes at roughly 10-second intervals; do not claim universal real-time delivery or use private Desktop IPC.
- [ ] **P1 — Verify skill onboarding in fresh clients ([#3](https://github.com/Jaemani/codex-monitor/issues/3)).** Fresh ordinary TUI native skill autocomplete passed, and the installed basic skill workflow passed in an owned CLI TUI for create/status/pause/resume/remove on one disposable managed file. Remaining: rerun against the final candidate runtime and skill, fresh Desktop discovery, a real producer, and natural-language reply/stop coverage without changing the target conversation.
- [ ] **P1 — Complete Desktop interaction cases.** Unsent draft, event during active response, approval waiting and cancellation recovery; retain actual user-visible observations.
- [ ] **P2 — Add an external adapter after conversation-scoped collector lifecycle is verified.** Authenticate the producer, filter relevant changes, preserve stable event IDs, and implement explicit source-scoped replies.

## Compatibility and resilience

- [ ] **Large-history resident recovery.** Registration now omits saved turns from resume
  responses with `excludeTurns: true`. Verify recovery on the reported deployment before closing
  the incident; the suspected WebSocket frame limit has not been confirmed by a measured response
  or close code. Do not replay its original inputs to test subscription recovery.
- [x] **CLI owner subscription primitive.** `resident` registers explicit existing tasks on one
  owner, retains the connection and restores subscriptions after transport loss. `connect --thread`
  returns to the exact same task through that owner. Read-only probes do not create model turns.
- [ ] **CLI resident operational validation.** Complete real ordinary-TUI multi-task, disconnected
  UI, owner restart, human approval, long idle and OS supervision cases. Two actual TUI-created tasks,
  closed-UI delivery, same-owner reconnect and owner-down backlog/restart now pass bounded native
  checks. Remaining: human approvals, long idle, OS startup/sleep/reboot and dashboard integration
  for resident health. A foreground command alone is not automatic startup after reboot.

- [ ] **P1 — Unloaded conversation ownership.** Shared-local persistence cannot automatically load
  arbitrary Desktop tasks. A real accepted/queued event with a `notLoaded` target exposed this operational
  gap. Keep its receipt intact; actual single consumption and substantive reply remain unresolved.
  Implement a supported persistent-owner lifecycle without forced turns, replay or model polling.
  Same-owner subscription is required; an isolated native test confirmed competing-writer rejection.
  See [required architecture and acceptance criteria](OWNER-LIFECYCLE.md).
  Prior multi-conversation delivery results do not establish unloaded-task wakeup.

- [ ] **P1 — Version compatibility probe and matrix.** Structured doctor diagnostics now distinguish missing queue methods and consumer readiness. Actual isolated Codex 0.147.0 rejects `thread/queue/list`; 0.153.4 remains the tested baseline. Broader versions and affected Windows-host recovery remain unverified.
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
- [x] 151 regression tests in hosted macOS/Linux CI and official skill/plugin format validators.

Raw transcripts remain local. See [public evidence](evidence/README.md) and [compatibility](COMPATIBILITY.md).
