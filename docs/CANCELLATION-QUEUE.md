# Native queue behavior after user interruption

Verified against Codex CLI `rust-v0.153.4`, commit
[`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`](https://github.com/openai/codex/tree/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a).

## Finding

If the user presses Ctrl+C at a permission prompt, external input already stored in the native queue remains queued. This is expected Codex behavior, not monitor delivery failure or data loss. The queue contract starts the next item automatically after a completed or failed turn, but leaves it waiting after an interrupted turn. `thread/resume` does not process it automatically, and a normal `turn/start` does not consume the queued item directly.

- [App Server queue contract](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/app-server/README.md#L884-L906)
- [Integration test preserving the queue after interrupt across hot and cold resume](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/app-server/tests/suite/v2/thread_queue.rs#L559-L695)

In the real TUI test, the queued event did not approve the pending command and no marker file was created. The event remained in the native queue after Ctrl+C. The first run incorrectly treated this as an automatic-processing failure, so its evidence remains preserved as [tui-control FAIL](evidence/README.md). In the follow-up test, the user submitted a new prompt; after that normal turn finished, Codex processed the waiting event once: [tui-control follow-up PASS](evidence/README.md).

## Cause

Ctrl+C in the permission overlay cancels the exec or patch request. App Server converts the cancellation to an abort, and Core interrupts the turn.

- [TUI permission cancellation](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/tui/src/bottom_pane/approval_overlay.rs#L491-L535)
- [App Server cancellation-to-abort conversion](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/app-server/src/bespoke_event_handling.rs#L2101-L2108)
- [Core abort-to-interrupt handling](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/handlers.rs#L199-L212)

The queue extension does not begin an external wake while the agent status is `Interrupted`, and it does not dispatch when the idle cause is `Interrupted`.

- [External wake blocked for interrupted status](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/ext/queue/src/service.rs#L472-L482)
- [Queue dispatch blocked for interrupted idle](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/ext/queue/src/service.rs#L534-L566)

When a later turn completes or fails, Core emits a non-interrupted idle lifecycle event. The queue extension then starts the head item and removes it after Core accepts the turn.

- [Core idle cause and lifecycle](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/tasks/mod.rs#L797-L866)
- [Queue-head dispatch and removal after acceptance](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/ext/queue/src/service.rs#L405-L469)

## Recovery and diagnosis

In the TUI, submit a new instruction and allow that normal user turn to finish. Enter is the default submit binding and starts a regular user turn. This TUI version has no action that directly invokes `thread/queue/start`.

- [Enter submit keymap](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/tui/src/keymap.rs#L1435-L1452)
- [Normal composer submission](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/tui/src/chatwidget/input_flow.rs#L15-L55)

The monitor never calls `thread/queue/start`, `thread/resume`, or `turn/start`; doing so could override the user's interruption. Instead, `codex-monitor inspect DELIVERY_ID` performs a bounded, read-only scan of the local receipt and native queue/history:

- `queued`: the client ID is currently in the native queue. The public API does not expose a queue-paused field, so this state alone does not prove that interruption paused it.
- `consumed`: the client ID appears in thread history.
- `unknown`: neither scan found the client ID. This does not prove non-delivery.
