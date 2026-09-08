# Acceptance criteria in real user environments

The product succeeds when a real external event reaches the same conversation without disrupting the user's work or input. The CLI TUI is the primary environment. Desktop and every other supported Codex surface require their own evidence.

## Environment coverage

| Environment | Delivery path | Required evidence |
|---|---|---|
| Local CLI TUI | `shared-local` | Real TUI input and output, same thread, exit and resume |
| Local Desktop | `shared-local` | Event input and response in the real app conversation, followed by user input |
| CLI TUI with `--remote` | Direct adapter to its WebSocket or Unix endpoint | Real TUI behavior, loaded-thread ownership, connection loss and recovery |
| IDE or another local owning client | `shared-local` with the same storage | Native queue consumption by that client |
| CLI/Desktop project on an SSH host | Host-local `shared-local` or SSH daemon proxy | Real host and client connection, permissions, and restart |
| Non-interactive CLI | A path supported by that process's persisted queue lifecycle | Reception during the process lifetime; post-exit retention is a separate result |
| Linux, Windows, or WSL | Real CLI and service provider for that OS | Installation, process locking, restart, input, and output on that OS |

A fake or protocol-level test on one surface does not replace real UI evidence on another. Test every available environment directly and record the concrete prerequisites for environments that are unavailable.

## Normal and concurrent behavior

1. User prompt, response, external event, event response, and user follow-up all remain in one thread.
2. An event arriving during a user turn waits without interruption and preserves the existing user queue order.
3. An event arriving while a draft is present preserves the draft and does not submit it.
4. Idle time without an event creates no model turn.
5. Duplicate events are delivered once; reused IDs with different content, failed authentication, and source mismatches are rejected clearly.
6. A failed or uncertain binding does not corrupt another binding's records or order.
7. The monitor does not approve a permission request, override a user interrupt, or force a cancelled task to resume.

## Restart and failure behavior

1. Clean wheel installation and reinstallation preserve credentials and state and report invalid environments clearly.
2. Receiver shutdown, `SIGKILL`, and restart preserve receipts and deduplication state.
3. Writer disconnects, timeouts, and process exits recover automatically when safe; ambiguous deliveries remain held for inspection.
4. An event accepted while the client is closed remains queued and is later handled in the same conversation.
5. Service install, start, stop, forced-exit recovery, restart, and removal preserve state and unrelated services.
6. A soak of at least one real hour mixes events, idle periods, restarts, and failures.
7. App restart, sleep, network loss, and OS reboot count as verified only after performing those exact actions.

## Evidence rules

- Record versions, environment, elapsed time, cases, failures, and fixes.
- `accepted` confirms storage in the official queue. Verify model response and task success separately.
- Compare the real TUI screen with the official transcript so prompt echo is not mistaken for a response.
- Record Desktop transcript evidence separately from direct screen observation.
- Identify and clean up every test-owned thread, process, and service.
- After a failure is fixed, rerun the same real failure path and retain the original failure record.
