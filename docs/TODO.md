# Project TODO

Updated 2026-10-07. This file tracks unfinished work; completed implementation and
historical measurements live in [STATUS.md](STATUS.md) and the
[verification summary](evidence/README.md). Python remains frozen reference code;
new runtime work belongs in `rust/`.

## Current follow-up

- [ ] Implement Linux dashboard permission changes with preview, shared-owner
  scope, rollback and effective-permission verification. Linux reconnect is
  implemented; Full Access switching is not. Never grant OS sudo privileges as
  a side effect.
- [ ] Validate Linux reconnect against a real idle Codex conversation, including
  account access and restored resident subscriptions. The disposable systemd
  test and read-only configuration match are not end-to-end reconnect evidence.
- [ ] Observe a real pending approval and user-input question through the owning
  client and verify the dashboard clears attention after the user responds.
- [ ] Verify visible notifications on a real SSH client, including terminal/OS
  permissions and tmux forwarding. Local PTY checks verify emitted OSC 9/BEL,
  not a displayed popup. Live-dashboard notification support is implemented;
  alerts while the dashboard is closed are not implemented.
- [ ] Review hosted CI results after integrating the Rust runtime into main.
  Local tests and publication checks do not establish hosted macOS validation.
- [ ] Complete the longer production soak and end-to-end Discord reply checks
  described in [status](STATUS.md); retire any remaining legacy dashboard only
  after its child Codex client has exited.

## Linux readiness acceptance

- [ ] Validate reboot recovery in an infrastructure-approved maintenance window.
- [ ] Validate Linux dashboard interaction and implement the remaining receiver
  service and permission controls before claiming parity with macOS. The new
  login-reload dashboard flow still needs acceptance; the older isolated CLI
  owner-reconnect canary is recorded separately in [TESTING.md](TESTING.md).
- [ ] Verify project-specific filesystem permissions, source adapters, real
  replies and a populated-state restore before approving a production cutover.
- [ ] Establish bounded log/data retention and repeat resource measurements with
  the intended production workload.

## Existing issue backlog

The previously unchecked items below are retained for continuity. Issue references
were checked on 2026-10-07; none was closed by this cleanup. Historical Python
installer follow-ups need reassessment against the canonical native runtime,
not a new Python-only fix. Completed checklist entries and repeated deployment
narratives have been removed from this task list; their evidence remains in
[STATUS.md](STATUS.md), [TESTING.md](TESTING.md) and Git history.

- [ ] Add harness-to-receiver leases for detached canaries, including abrupt harness death.

- [ ] Expose process spawn/reap counters and runtime identity in service diagnostics.

- [ ] Automate retained-interpreter provisioning and pre-upgrade runtime validation in the installer.

- [ ] **Rust service lifecycle parity.** ([#14](https://github.com/Jaemani/codex-monitor/issues/14)) Native migration and macOS service adoption are implemented. Complete Linux receiver service installation, login/reboot/sleep validation and remaining migration edge cases.

- [ ] **Execution and attention acceptance.** ([#14](https://github.com/Jaemani/codex-monitor/issues/14)) Read-only authentication/execution errors and approval/input flags are implemented. Verify the remaining real-client failure, pending-decision and recovery cases without health-check model turns.

- [ ] **Close out the Rust evaluation issue.** ([#14](https://github.com/Jaemani/codex-monitor/issues/14)) Rust adoption and bounded comparisons are complete. Reconcile the remaining service, migration, memory-footprint and endurance criteria against the recorded evidence before closing the issue.

- [ ] **External producer health integration.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Surface observed connection state and freshness from
  authenticated producers. Receiver liveness, REST credentials and an old successful receipt must not
  imply a currently connected event stream. Preserve unknown until observation exists.

- [ ] **P1 — Manage event producers ([#2](https://github.com/Jaemani/codex-monitor/issues/2)).** Managed local files now expose observed collector state and checkpoint recovery under the receiver. Bounded process isolation passed installed failure/recovery checks. Remaining: broader CI/webhook producers, longer endurance coverage and OS sleep/reboot. A configured binding must not imply an active watch.

- [ ] **P1 — Conditional monitor policies (level 4).** ([#8](https://github.com/Jaemani/codex-monitor/issues/8)) Durable debounce and bounded JSON predicates passed installed macOS/Linux process checks, including invalid samples, restart/pause timing reset and independent conversations. A 300-second unchanged-condition soak passed. Actual ordinary default shared-local and Unix remote TUI matched/recovery events and user follow-up passed with exact client-ID correlation; escalation policies remain future work. Keep evaluation outside the model.

- [ ] **P2 — Request retention and archival.** ([#9](https://github.com/Jaemani/codex-monitor/issues/9)) Provide explicit bounded archival with a documented deduplication horizon; preserve unresolved native delivery evidence. Current records are retained up to the store capacity.

- [ ] **P1 — Reduce event latency through supported APIs ([#1](https://github.com/Jaemani/codex-monitor/issues/1)).** Managed monitors now accept explicit owner endpoints. A six-sample actual Unix `codex --remote` TUI run passed: idle 0.793–1.150 seconds, post-receiver-restart 0.585–0.633 seconds, busy generation 30.005–33.051 seconds. Two samples per scenario are not a dependable tail estimate. Broader workloads and Desktop owner paths remain. See [technical latency comparison](LATENCY.md). Shared-local currently observes external changes at roughly 10-second intervals; do not claim universal real-time delivery or use private Desktop IPC.

- [ ] **P1 — Verify skill onboarding in fresh clients ([#3](https://github.com/Jaemani/codex-monitor/issues/3)).** Fresh ordinary TUI native skill autocomplete passed, and the installed basic skill workflow passed in an owned CLI TUI for create/status/pause/resume/remove on one disposable managed file. Remaining: rerun against the final candidate runtime and skill, fresh Desktop discovery, a real producer, and natural-language reply/stop coverage without changing the target conversation.

- [ ] **P1 — Complete Desktop interaction cases.** ([#3](https://github.com/Jaemani/codex-monitor/issues/3)) Unsent draft, event during active response, approval waiting and cancellation recovery; retain actual user-visible observations.

- [ ] **P2 — Broader external adapters.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Native Discord Gateway support is implemented. Add other producers with authentication, relevance filtering, stable event IDs and explicit source-scoped replies as needed.

- [ ] **Large-history resident recovery.** ([#6](https://github.com/Jaemani/codex-monitor/issues/6)) Registration now omits saved turns from resume
  responses with `excludeTurns: true`. Verify recovery on the reported deployment before closing
  the incident; the suspected WebSocket frame limit has not been confirmed by a measured response
  or close code. Do not replay its original inputs to test subscription recovery.

- [ ] **CLI resident operational validation.** ([#5](https://github.com/Jaemani/codex-monitor/issues/5)) Complete real ordinary-TUI multi-task, disconnected
  UI, owner restart, human approval, long idle and OS supervision cases. Two actual TUI-created tasks,
  closed-UI delivery, same-owner reconnect and owner-down backlog/restart now pass bounded native
  checks. Remaining: human approvals, long idle, OS startup/sleep/reboot and dashboard integration
  for resident health. A foreground command alone is not automatic startup after reboot.

- [ ] **P1 — Unloaded conversation ownership.** ([#4](https://github.com/Jaemani/codex-monitor/issues/4)) Shared-local persistence cannot automatically load
  arbitrary Desktop tasks. A real accepted/queued event with a `notLoaded` target exposed this operational
  gap. Keep its receipt intact; actual single consumption and substantive reply remain unresolved.
  Implement a supported persistent-owner lifecycle without forced turns, replay or model polling.
  Same-owner subscription is required; an isolated native test confirmed competing-writer rejection.
  See [required architecture and acceptance criteria](OWNER-LIFECYCLE.md).
  Prior multi-conversation delivery results do not establish unloaded-task wakeup.

- [ ] **P1 — Version compatibility probe and matrix.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Structured doctor diagnostics now distinguish missing queue methods and consumer readiness. Actual isolated Codex 0.147.0 rejects `thread/queue/list`; 0.153.4 remains the tested baseline. Broader versions and affected Windows-host recovery remain unverified.

- [ ] **P1 — Windows/WSL.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Validate runtime transports and ordinary TUI. Current POSIX installer refuses Windows; do not advertise installer parity.

- [ ] **P2 — Desktop remote/SSH.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Test an explicitly configured remote host and exact conversation ownership.

- [ ] **P2 — OS sleep and reboot.** ([#7](https://github.com/Jaemani/codex-monitor/issues/7)) Verify receiver/producer restart, credential preservation and same-conversation event recovery.

- [ ] **P2 — Public webhook deployment.** ([#2](https://github.com/Jaemani/codex-monitor/issues/2)) Validate a selected reverse proxy, authentication, body/rate limits and outage recovery.

- [ ] **P2 — Matched Claude comparison.** ([#13](https://github.com/Jaemani/codex-monitor/issues/13)) Use the same event workloads, restart/failure cases and latency measurements. Document adapter differences; no overall superiority claim before evidence.

- [ ] **P2 — Publish a versioned release.** ([#10](https://github.com/Jaemani/codex-monitor/issues/10)) Rebuild and verify the final archive, document dependencies and checksums, and decide release support policy. A public repository is not a package-registry release.

- [ ] **P2 — Choose a license.** ([#11](https://github.com/Jaemani/codex-monitor/issues/11)) Public source visibility alone does not grant an open-source license.

- [ ] **P2 — Upgrade/rollback usability.** ([#12](https://github.com/Jaemani/codex-monitor/issues/12)) Simplify receiver migration across runtime releases while preserving explicit ownership and state.
