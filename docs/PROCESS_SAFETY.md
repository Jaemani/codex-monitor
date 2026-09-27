# File-sampler process ownership

## Native Rust runtime

The authoritative receiver uses a reusable pool capped at four native sampler
children. Each child is owned by its supervisor, receives bounded jobs over pipes,
and is replaced only through controlled cleanup. Reads are limited to regular
files and 8 MiB, with a two-second parent deadline. Kernel-stuck I/O remains outside
user-space termination guarantees. Native pool contracts are exercised by
`cargo test --locked --manifest-path rust/Cargo.toml`.

The section below describes the frozen Python implementation and historical
incident fixes; its eight-worker limit and interpreter requirements do not apply
to native Rust production services.

## Legacy Python reference

The Python receiver launches each file sample with an explicitly owned
`subprocess.Popen` handle. It does not use multiprocessing startup serialization.
An interpreter that exits before initialization still has a handle that the
receiver polls and reaps. Job encoding happens before spawning; no parent-side
bootstrap write can lose an already-created child.

Ownership is registered before result processing. Setup exceptions terminate
and wait for the child. Normal completion, timeout, pause, removal, and shutdown
all use the same worker registry. A terminated child remains in capacity
accounting until it is observed dead and reaped. Limits remain eight concurrent
workers per receiver and two per conversation, with a two-second sample deadline.

The child reads a lifetime lease on stdin. Receiver death closes the pipe and
causes the initialized sampler to exit, including when its sampling function is
blocked. Results are capped at 4096 bytes. The parent reads only after exit and
readiness and does not wait indefinitely for a descendant to close stdout.

## Failed startup

A launch exception or missing/malformed child result opens a receiver-wide
circuit. New sampler launches stop across all watches, including newly created
watches. Status exposes `file sample spawning suspended` with a recovery hint.
Pending durable events can still be replayed through ordinary intake; delivery
and authentication semantics are unchanged.

Repair the interpreter, dependencies, permissions, or resource shortage before
restarting the receiver. Restart creates a new supervisor and clears the circuit.
Do not repeatedly restart an unhealthy runtime. Raising process limits or
ignoring SIGCHLD is not a substitute for repairing ownership.

Use a retained interpreter for installed services. A virtual environment may
still reference a versioned package-manager interpreter and its shared libraries;
removing that runtime can break both new workers and the service launcher while
an older parent remains alive. Verify the installed interpreter and imports
before starting the service after maintenance.

## Verification and operational guard

Run the bounded real-child regression tests:

```sh
python3 -m unittest tests.test_sample_lifecycle tests.test_managed -v
```

They cover an interpreter exiting before registration, global admission failure,
an exception after spawn but before ownership setup completes, timeout cleanup,
and receiver death. Reap assertions check that the original child is no longer
waitable by its parent, rather than only checking the size of the registry.

Observe actual OS children as well as the worker registry. A sustained increase
in zombies, especially with an empty worker registry, warrants immediate
diagnostic capture and admission shutdown. Record PID, process start time,
parent/group, state directory, interpreter path, and release identity before
restarting a suspected parent. Never broadly reap another component's children
with `waitpid(-1)` or kill unrelated Python processes by name.

These checks exercise local process behavior. They do not prove real Codex model
completion, historical incident attribution, or cleanup of every descendant
created by an external tool. Detached test receivers still need their harness to
run cleanup; a killed harness can leave its receiver alive. An OS process stuck
in an uninterruptible kernel operation is outside user-space termination
guarantees and must continue to consume a capacity slot.

## When a connected agent stops responding

An enabled route is configuration, and queue acceptance only confirms delivery.
The dashboard probes each explicit owner's conversation independently, within
its existing time budget and cache. `systemError` requires inspecting execution
errors; it is not classified as ready. Credential presence does not validate a
token. A paused, unloaded route is not itself an outage.

A delivery unresolved for at least one hour is highlighted even if a newer
message was accepted. Uncertain delivery requires receipt inspection before any
replay. These are display warnings, not automatic expiry, retry or notifications.

Inspect the selected conversation's execution error first. Repair authentication
in the actual owner environment for authentication failures. Restart only the
failed component when needed, then verify an authorized message through model
completion and remote reply delivery. Preserve old receipts and deduplicate any
approved replay. Integrations should emit explicit request failure/completion
reports; a healthy receiver cannot infer remote reply success.
