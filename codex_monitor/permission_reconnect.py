"""Explicit, backed-up permission changes for verified local owner services."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import plistlib
import tempfile
import time

from .owner_reconnect import ReconnectError, _option

MODES = {"full": "danger-full-access", "read-only": "read-only", "workspace-network": "workspace-write"}


def policy_summary(plan):
    configured = sorted({j.args[i + 1] for j in plan.residents
                         for i, v in enumerate(j.args[:-1]) if v == "--thread"})
    previous = sorted({_option(j.args, "--sandbox") or "native defaults" for j in plan.residents})
    description = {"full": "Full access: unrestricted filesystem and network commands.",
                   "read-only": "Read-only: filesystem writes and command networking disabled; reply/setup commands may fail.",
                   "workspace-network": "Project Access: project writes and outbound network; external setup paths remain restricted."}[plan.policy]
    return (description + " Applies to ALL " + str(len(configured)) + " configured conversations on this shared owner: "
            + ", ".join(configured) + ". Current resident modes: " + ", ".join(previous)
            + ". Saved service defaults will change and survive restarts. This is not temporary; select another mode to restore restrictions. "
            "Existing terminals disconnect. Select the SAME permission action again within 60 seconds to confirm.")


def policy_args(args, policy, *, resident=False):
    """Replace only permission overrides, preserving endpoint and unrelated options."""
    mode = MODES[policy]
    out, index = [], 0
    permission_keys = {"sandbox_mode", "sandbox_workspace_write.network_access", "default_permissions", "permissions"}
    while index < len(args):
        value = args[index]
        if value in {"--sandbox", "-s"}:
            if index + 1 >= len(args):
                raise ReconnectError("Malformed sandbox service argument.")
            index += 2
            continue
        if value.startswith("--sandbox="):
            index += 1
            continue
        if value in {"--network-access", "--no-network-access", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto"}:
            index += 1
            continue
        if not resident and value in {"-c", "--config"}:
            if index + 1 >= len(args):
                raise ReconnectError("Malformed owner config argument.")
            key = args[index + 1].split("=", 1)[0].strip()
            if key in permission_keys or key.startswith("permissions."):
                index += 2
                continue
            out.extend(args[index:index + 2]); index += 2
            continue
        if not resident and (value.startswith("--config=") or (value.startswith("-c") and value != "-c")):
            # Do not guess at compact or ambiguous overrides during a service rewrite.
            raise ReconnectError("Normalize compact owner configuration arguments before changing permissions.")
        out.append(value); index += 1
    if resident:
        out += ["--sandbox", mode]
        if policy == "workspace-network":
            out += ["--network-access"]
    else:
        out[1:1] = ["-c", f'sandbox_mode="{mode}"', "-c",
                    "sandbox_workspace_write.network_access=" + ("true" if policy == "workspace-network" else "false")]
    return tuple(out)


def _write(path, raw, mode):
    fd, temporary = tempfile.mkstemp(prefix=".permission-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _bootstrap(backend, job):
    # launchd can briefly reject bootstrap immediately after a successful bootout.
    for attempt in range(4):
        try:
            backend._launchctl("bootstrap", f"gui/{os.getuid()}", str(job.path))
            return
        except ReconnectError:
            try:
                backend._pid(job)
                return
            except ReconnectError:
                if attempt == 3:
                    raise
                backend.sleep(.5)


def _verify(backend, plan, jobs, expected):
    deadline = backend.clock() + 30
    last = "waiting for resident subscriptions"
    while backend.clock() < deadline:
        rpc = None
        try:
            backend._pid(jobs[0])
            rpc = backend._rpc(plan.endpoint)
            if rpc.call("account/read", {"refreshToken": False}).get("account") != expected:
                raise ReconnectError("Restarted owner account differs from the verified login.")
            loaded = backend._threads(rpc, deadline)
            if not set(plan.threads).issubset(loaded):
                backend.sleep(.25)
                continue
            for thread in plan.threads:
                # Rejoin only after resident loaded it; never create a model turn.
                response = rpc.call("thread/resume", {"threadId": thread, "excludeTurns": True},
                                    timeout=backend._timeout(deadline))
                policy = response.get("sandbox", {})
                expected_type = {"full": "dangerFullAccess", "read-only": "readOnly", "workspace-network": "workspaceWrite"}[plan.policy]
                if policy.get("type") != expected_type or (plan.policy != "full" and
                        policy.get("networkAccess") is not (plan.policy == "workspace-network")):
                    raise ReconnectError("Effective conversation permissions do not match the selected mode.")
            return
        except Exception as error:
            last = str(error) if isinstance(error, ReconnectError) else "owner verification unavailable"
        finally:
            if rpc:
                rpc.close()
        backend.sleep(.25)
    raise ReconnectError(last)


def execute_policy(backend, plan, expected):
    originals, changed = {}, []
    jobs = (plan.owner, *plan.residents)
    for index, job in enumerate(jobs):
        raw = job.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != job.digest:
            raise ReconnectError("Service changed after preview; permission change cancelled.")
        args = policy_args(job.args, plan.policy, resident=index > 0)
        spec = plistlib.loads(raw); spec["ProgramArguments"] = list(args)
        updated = plistlib.dumps(spec)
        originals[job.path] = (raw, job.path.stat().st_mode & 0o777)
        changed.append(replace(job, args=args, digest=hashlib.sha256(updated).hexdigest()))
    # Fail before stopping services if an installed resident lacks these options.
    for job in plan.residents:
        result = backend.runner([job.args[0], "resident", "--help"], capture_output=True, text=True, timeout=5)
        if result.returncode or "--sandbox" not in result.stdout or (plan.policy == "workspace-network" and "--network-access" not in result.stdout):
            raise ReconnectError("Upgrade the resident runtime before changing permissions.")
    backup = backend.root / "permission-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex())
    backup.mkdir(parents=True, mode=0o700)
    for job in jobs:
        _write(backup / job.path.name, originals[job.path][0], 0o600)
    stopped = []
    try:
        for job in (*plan.residents, plan.owner):
            stopped.append(job)
            backend._launchctl("bootout", backend._domain(job))
        for job in changed:
            spec = plistlib.loads(originals[job.path][0]); spec["ProgramArguments"] = list(job.args)
            _write(job.path, plistlib.dumps(spec), originals[job.path][1])
        for job in changed:
            _bootstrap(backend, job)
        _verify(backend, plan, changed, expected)
    except Exception as error:
        rollback_ok = True
        # Restore only jobs actually involved in this transaction.
        for job in reversed(stopped):
            try:
                backend._launchctl("bootout", backend._domain(job))
            except ReconnectError:
                pass
        for job in jobs:
            try:
                _write(job.path, *originals[job.path])
            except OSError:
                rollback_ok = False
        for job in jobs:
            if job in stopped:
                try:
                    _bootstrap(backend, job)
                except Exception:
                    rollback_ok = False
        detail = str(error) if isinstance(error, ReconnectError) else "service update failed"
        raise ReconnectError(detail + (" Previous service configuration restored; inspect effective owner state." if rollback_ok else
                             " Automatic restoration incomplete; inspect services before retrying.") + f" Backup: {backup}") from None
    return (f"Reconnected in {plan.policy} mode; verified permissions for {len(plan.threads)} conversations. "
            f"Service defaults persist until changed. No input replayed. Backup: {backup}")
