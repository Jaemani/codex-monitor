# Findings for an independent account manager

## Canonical runtime

Use native Rust `codex-monitor` 0.2.0+. The default state path and source credentials
are retained through explicit offline migration. Python is a historical reference.
Permission changes currently apply to the shared owner and its residents, not one
conversation. Identify the server and all affected conversations before applying.
Account switching and permission changes remain separate operations. `headroom` is
optional; never add or require it as part of monitor/account integration.

These findings come from codex-monitor's local authentication recovery work on
2026-09-27. They inform a separate account/runtime manager; they do not make that
manager a required dependency of codex-monitor.

## Authentication belongs to a runtime as well as a stored profile

A running owner and a newly started App Server reported different account
identities while using the user's usual Codex storage. The new process could
access the account API; the old process had failed token refresh after an account
change. A token refresh request to the old owner was insufficient to adopt the new
login. Distinguish the saved account, observed runtime account and conversation.
Do not infer a running process's account from the current login file alone.

Keep profiles and authentication storage separate for concurrent accounts. A
user-selected account change should be an explicit runtime transition, not a
shared auth-file replacement assumed to update every live process.

## Account identity and model provider are separate choices

No custom provider, including `headroom`, is mandatory. That name appeared in one
existing conversation's saved metadata; its definition was absent from current
configuration. Seven conversations resumed after an owner restart, while this
one could not resume. It was a provider-configuration failure, not a reason to
require that provider for all accounts or users.

Preserve the saved provider when its configuration is available. Resolve
effective configuration in the conversation's project directory, including
project layers. A definition found for one project must not validate another.
For missing definitions, explain the mismatch and let the user restore the
definition or explicitly choose another provider. Never silently install a named
provider or replace it with OpenAI. Provider configuration presence is separate
from valid provider credentials and successful model execution.

The monitor's initial conservative reconnect guard rejected every custom provider.
It was corrected to permit configured custom providers. Built-in OpenAI requires
no custom provider definition. This reconnect flow still specifically verifies a
saved ChatGPT login; it is not a general validator for all authentication modes.

## Shared owners require coordinated recovery

One owner served eight existing conversations, retained by two resident services.
Restarting the owner affects every attached conversation and connected UI. Before
recovery, enumerate exact conversation IDs, check for active or unverified states,
verify saved tasks and provider configuration, and confirm resident coverage.
Do not resume the same conversation concurrently on a competing owner.

Idle checks are observations, not an atomic barrier against arriving work. An
account manager should own admission and recovery together so it can stop new
assignments, coordinate existing work, perform the transition, and restore
admission afterward. A monitoring client should consume its readiness API rather
than independently restart the same runtime.

## A restart request can succeed despite a client timeout

In the live recovery, launchctl exceeded the command timeout after accepting the
restart. The process had already changed and account access worked. Verify the
new PID, expected account and restored conversations after an uncertain response;
do not automatically send another restart. Use a cross-client lock or durable
operation identity to prevent competing recoveries.

## Recovery has several distinct success criteria

Track these separately:

- Transport reachable.
- Runtime account matches the selected saved account.
- Authenticated account API access succeeds.
- Expected saved conversations are restored.
- A model turn succeeds.
- The user's intended work completes.

Account presence alone is not authentication validation. A usage API response is
not proof of available quota or successful model work. Restoring a conversation
does not replay its failed input. Avoid automatic replay without checking prior
tool side effects and delivery identity.

## Evidence and implementation references

Live evidence established current-account adoption, authenticated account access
and restoration of seven of eight original conversations. The remaining task had
missing provider configuration; no automatic fallback was applied. Recovery did
not submit a model turn, so completed model work remains unverified.

Tests cover stale previews, busy tasks, incomplete resident coverage, service
identity changes, cross-dashboard exclusion, missing saved tasks, account mismatch,
restart timeout reconciliation and optional project-scoped providers. Fake-owner
and PTY checks are separate from the live observations above.

- [Reconnect implementation](../codex_monitor/owner_reconnect.py)
- [Reconnect regression tests](../tests/test_owner_reconnect.py)
- [Dashboard behavior and supported scope](DASHBOARD.md)
- [Official App Server interfaces](https://learn.chatgpt.com/docs/app-server)

The monitor's current restart adapter is limited to verified local macOS user
LaunchAgents with direct Codex owners and resident services. An independent
account manager should keep platform supervision behind its own interface.

## Preserve execution permissions across owner replacement

Authentication recovery is not permission recovery. On the tested Codex 0.157.1
resume path, legacy sandbox state without a named permission profile can revert
to current configuration defaults. Set the intended owner policy explicitly and
verify the returned sandbox and network fields after reconnect. Discord reply
commands can use workspace-write with network access enabled; full access is not
required. Do not infer working network permissions from successful account reads,
a live gateway, or CLI flags forwarded to a remote TUI. See
[the root-cause report](RESUME-PERMISSION-ROOT-CAUSE.md) and
[owner configuration](OWNER-LIFECYCLE.md#discord-reply-permissions).

## Provisioning permission choice and dashboard handoff

Before creating Discord threads/channels and their worktree, conversation, route
or resident configuration, perform a mandatory permission preflight. Show the
required filesystem paths, network/API permissions and service operations; obtain
the user's scope choice unless already explicitly authorized. A working reply
path is not proof of provisioning access. Offer targeted access, temporary full
access with restoration, or keeping the current restrictions. Full access is not
a Discord API requirement and cannot replace bot permissions.

Monitor's dashboard separates ordinary Reconnect from a Change Permission menu
with Full Access, Read-only and Project Access for verified local macOS owners. The first action previews all configured
conversation IDs; the same second action within 60 seconds confirms. The mode is
persistent and applies to the shared owner and all matching residents, not just
one selected conversation. Read-only also disables command networking. Existing
service files are backed up, updates attempt rollback on failure, and returned
permissions are verified for previously loaded conversations. Queued or active
work blocks permission changes. No model turn or input replay is used to verify.

An account manager integrating this flow should keep account identity, execution
permissions, and remote-service permissions as separate states. Preserve and show
the previous policy, never infer full access from login success or a Discord source,
and explicitly restore restrictions after a temporary-access task. Do not advertise
automatic temporary restoration: the current dashboard requires another explicit
mode change. Permission controls are available features, not authorization to
change a live user's mode during account switching.

## Native Gateway and UI lifecycle

Discord producer services can run `codex-monitor discord-gateway` with separate
adapter configs and SQLite state. Their identity/readiness checks and cutover
protocol are documented in [DISCORD-GATEWAY.md](DISCORD-GATEWAY.md). They do not
inherit a dashboard project's permission scope or require a named provider.
Preserve the selected account, owner and project routing during any integration.
A dashboard can be replaced independently, but do not terminate an old dashboard
while it parents an active Codex TUI. `/quit` detaches that TUI back to its waiting
dashboard; it does not stop the shared owner or the monitoring services.
