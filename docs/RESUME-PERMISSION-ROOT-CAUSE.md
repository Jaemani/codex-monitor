# Resume permission root cause (2026-09-27)

## Confirmed reproduction

The affected saved conversation had legacy `sandbox_policy.type =
"danger-full-access"` and no `active_permission_profile` before the failed turn.
The user configuration had no sandbox or network override; its project was
trusted. No project configuration supplied a network override.

A disposable Codex 0.157.1 App Server resumed a copy of the pre-failure history,
without any TUI connected and without starting a model turn. Both cases used
the same fixture and an explicit built-in provider to avoid an unrelated missing
custom-provider definition in the historical fixture.

| Configuration | Returned sandbox | Authenticated Discord GET through command/exec |
| --- | --- | --- |
| Trusted project; no network override | workspaceWrite, networkAccess false | Failed with the bridge's original transport error |
| Same configuration plus sandbox_workspace_write.network_access true | workspaceWrite, networkAccess true | Bot and channel verification succeeded |

The command probe used the returned sandbox policy and the existing reply
adapter's read-only verify operation. No message was posted, no model turn was
started, and no live permission configuration was changed by this experiment.
An initial unauthenticated probe received HTTP 403 with networking enabled;
that was replaced with the actual adapter's authenticated GET verification.

## Why the default was selected

In the release source, `latest_persisted_resume_settings` extracts approval
policy, approval reviewer and active permission profile, not the legacy sandbox
policy. The resume loader restores the named profile ID only when one exists.
This historical conversation had no such profile. Configuration resolution
therefore selected the workspace profile for the trusted project, with networking
restricted when no network override was configured.

Sources for release rust-v0.157.1:

- [Saved resume settings](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/app-server/src/request_processors/persisted_resume_settings.rs)
- [Resume loader](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/app-server/src/request_processors/thread_processor.rs#L4186)
- [Default project permissions](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/src/config/permissions.rs#L51)
- [App Server CLI dispatch](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/cli/src/main.rs#L1219)

The owner's top-level interactive bypass flag was not converted to App Server
configuration by that dispatch path. A local probe confirmed it did not establish
the server defaults. Restart made the session reload exercise this behavior;
account authentication itself did not set networkAccess to false.

## Corrections to the earlier diagnosis

Concurrent remote terminals complicated recovery of an already loaded thread,
but are not necessary to reproduce the permission reset. They must not be
identified as its confirmed root cause. Full access restores connectivity but
is not required for Discord. Workspace permissions with networking enabled passed
the same authenticated read-only checks.

The remote TUI source also clears permission overrides in resume requests.
Forwarding a CLI sandbox option alone must not be described as proof that a
remote session will adopt that mode. Verify the actual server response. The
explicit resident resume RPC is a separate path and was verified previously.

The follow-up repair adds explicit workspace networking to resident registration
and removes misleading remote TUI flag forwarding. See the owner lifecycle guide
for the corrected setup. Live deployment verification is recorded in STATUS.md.
