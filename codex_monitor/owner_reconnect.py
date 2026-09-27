"""Explicit reconnect of a verified local launchd owner and its resident tasks.

No credential copying, account injection, process guessing or model turns.
Only a matching user LaunchAgent with resident coverage may be restarted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from .owner_health import _status_value, _supported_endpoint
from .session import Rpc, server_token


class ReconnectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Job:
    path: Path
    digest: str
    label: str
    args: tuple[str, ...]
    env: dict
    cwd: str | None


@dataclass(frozen=True)
class Plan:
    endpoint: str
    thread: str
    owner: Job
    residents: tuple[Job, ...]
    threads: tuple[str, ...]
    pid: int
    created: float
    policy: str | None = None

    def summary(self):
        if self.policy:
            from .permission_reconnect import policy_summary
            return policy_summary(self)
        return (f"Reconnect will restart one shared owner serving {len(self.threads)} conversations. "
                "Existing Codex windows will disconnect; queued input may run after reconnect. "
                "Select Reconnect again within 60 seconds to confirm. Conversations: "
                + ", ".join(self.threads))


def _option(args, name):
    values = [args[i + 1] for i, v in enumerate(args[:-1]) if v == name]
    return values[0] if len(values) == 1 else None


class OwnerReconnect:
    def __init__(self, root, *, jobs_dir=None, runner=subprocess.run, rpc_factory=Rpc,
                 clock=time.monotonic, sleep=time.sleep):
        self.root = Path(root)
        self.jobs_dir = Path(jobs_dir or Path.home() / "Library/LaunchAgents")
        self.runner, self.rpc_factory, self.clock, self.sleep = runner, rpc_factory, clock, sleep

    def _launchctl(self, *args):
        try:
            result = self.runner(["/bin/launchctl", *args], capture_output=True, text=True, timeout=5)
        except Exception as exc:
            raise ReconnectError("The owner service could not be contacted.") from exc
        if result.returncode:
            raise ReconnectError("The configured owner or resident service is not running.")
        return result.stdout

    def _domain(self, job):
        return f"gui/{os.getuid()}/{job.label}"

    def _pid(self, job):
        text = self._launchctl("print", self._domain(job))
        # Match the loaded job, not merely a similarly named plist on disk.
        path = re.search(r"^\s*path = (.+)$", text, re.M)
        args = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", text, re.M | re.S)
        pid = re.search(r"^\s*pid = (\d+)$", text, re.M)
        if (not path or Path(path[1]) != job.path or not args or
                tuple(line.strip() for line in args[1].splitlines() if line.strip()) != job.args or not pid):
            raise ReconnectError("Loaded service identity differs from its saved configuration.")
        return int(pid[1])

    def _jobs(self):
        if sys.platform != "darwin":
            raise ReconnectError("Reconnect currently supports verified macOS user LaunchAgents only.")
        jobs = []
        for path in sorted(self.jobs_dir.glob("*.plist"))[:256]:
            try:
                stat = path.lstat()
                if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o022 or stat.st_size > 128 * 1024:
                    continue
                raw = path.read_bytes()
                value = plistlib.loads(raw)
                args, label = value.get("ProgramArguments"), value.get("Label")
                if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
                    continue
                if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
                    continue
                env = value.get("EnvironmentVariables", {})
                if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                    continue
                jobs.append(Job(path, hashlib.sha256(raw).hexdigest(), label, tuple(args), env,
                                value.get("WorkingDirectory")))
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
        return jobs

    def _configuration(self, endpoint):
        if not _supported_endpoint(endpoint) or not (endpoint.startswith("unix:///") or
                endpoint.startswith(("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:"))):
            raise ReconnectError("Reconnect requires an explicit local owner endpoint.")
        jobs = self._jobs()
        owners = [j for j in jobs if Path(j.args[0]).name == "codex" and "app-server" in j.args
                  and _option(j.args, "--listen") == endpoint and "proxy" not in j.args]
        if len(owners) != 1:
            raise ReconnectError("No unique direct Codex owner LaunchAgent matches this connection.")
        owner = owners[0]
        expected_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        actual_home = Path(owner.env.get("CODEX_HOME", str(Path(owner.env.get("HOME", str(Path.home()))) / ".codex"))).expanduser().resolve()
        if actual_home != expected_home:
            raise ReconnectError("The owner uses another Codex home. Reconnect from that login environment.")
        if not Path(owner.args[0]).is_absolute():
            raise ReconnectError("The owner executable must be an absolute path.")
        residents = tuple(j for j in jobs if Path(j.args[0]).name == "codex-monitor" and
                          "resident" in j.args and _option(j.args, "--endpoint") == endpoint)
        if not residents:
            raise ReconnectError("No matching resident service can restore this owner's conversations.")
        for j in residents:
            self._pid(j)
        return owner, residents

    def _rpc(self, endpoint):
        return self.rpc_factory(endpoint, timeout=3, token=server_token())

    def _timeout(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ReconnectError("Owner inspection timed out; restart was not verified.")
        return min(3.0, remaining)

    def _threads(self, rpc, deadline=None):
        deadline = self.clock() + 10 if deadline is None else deadline
        threads, cursor, seen = set(), None, set()
        for _ in range(8):
            result = rpc.call("thread/loaded/list", {} if cursor is None else {"cursor": cursor},
                              timeout=self._timeout(deadline))
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ReconnectError("Could not verify the owner's loaded conversations.")
            for thread in result["data"]:
                if not isinstance(thread, str):
                    raise ReconnectError("The owner returned an unsupported conversation inventory.")
                threads.add(thread)
            cursor = result.get("nextCursor")
            if not cursor:
                return tuple(sorted(threads))
            if not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
        raise ReconnectError("The owner inventory exceeds the bounded reconnect check.")

    def _idle(self, rpc, threads, deadline):
        if len(threads) > 32:
            raise ReconnectError("Reconnect supports at most 32 loaded conversations per owner.")
        for thread in threads:
            value = rpc.call("thread/read", {"threadId": thread, "includeTurns": False},
                             timeout=self._timeout(deadline))
            status = _status_value((value.get("thread") or {}).get("status"))
            if status not in {"idle", "systemError"}:
                raise ReconnectError("A conversation is active, awaiting approval, or unverified. Retry after it is idle.")

    def plan(self, endpoint, thread, policy=None):
        if policy not in (None, "full", "read-only", "workspace-network"):
            raise ReconnectError("Unsupported reconnect permission mode.")
        owner, residents = self._configuration(endpoint)
        pid = self._pid(owner)
        rpc = self._rpc(endpoint)
        try:
            deadline = self.clock() + 12
            threads = self._threads(rpc, deadline)
            self._idle(rpc, threads, deadline)
            if policy:
                configured = {j.args[i + 1] for j in residents
                              for i, value in enumerate(j.args[:-1]) if value == "--thread"}
                for saved in sorted(set(threads) | configured):
                    queue = rpc.call("thread/queue/list", {"threadId": saved, "limit": 1},
                                     timeout=self._timeout(deadline))
                    if not isinstance(queue.get("data"), list) or queue["data"]:
                        raise ReconnectError("Permission changes require an empty queue for every loaded conversation.")
        finally:
            rpc.close()
        covered = {j.args[i + 1] for j in residents for i, value in enumerate(j.args[:-1]) if value == "--thread"}
        if thread not in threads or not set(threads).issubset(covered):
            raise ReconnectError("Resident services do not cover every loaded conversation; owner was not restarted.")
        return Plan(endpoint, thread, owner, residents, threads, pid, self.clock(), policy)

    def _fresh_account(self, plan):
        args = list(plan.owner.args)
        args[args.index("--listen") + 1] = "stdio://"
        env = os.environ.copy()
        env.update(plan.owner.env)
        rpc = self.rpc_factory(command=args, env=env, cwd=plan.owner.cwd, timeout=5)
        try:
            deadline = self.clock() + 15
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt" or not account.get("email"):
                raise ReconnectError("The current saved ChatGPT login cannot be verified. Sign in first.")
            limits = rpc.call("account/rateLimits/read", {})
            if not isinstance(limits, dict) or not any(isinstance(limits.get(k), dict)
                                                      for k in ("rateLimits", "rateLimitsByLimitId")):
                raise ReconnectError("Current login access could not be verified; owner was not restarted.")
            # Check that the configured storage still contains every saved task.
            # This is a read, not a competing resume or a subscription.
            providers_by_cwd = {}
            for thread in plan.threads:
                result = rpc.call("thread/read", {"threadId": thread, "includeTurns": False},
                                  timeout=self._timeout(deadline))
                if (result.get("thread") or {}).get("id") != thread:
                    raise ReconnectError("A saved conversation is missing from owner storage; restart cancelled.")
                saved = result["thread"]
                provider = saved.get("modelProvider")
                if provider == "openai":
                    continue
                cwd = saved.get("cwd")
                if not isinstance(provider, str) or not provider or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                    raise ReconnectError("A saved conversation's provider or project directory is unknown; owner was not restarted.")
                if cwd not in providers_by_cwd:
                    try:
                        config = rpc.call("config/read", {"cwd": cwd, "includeLayers": False},
                                          timeout=self._timeout(deadline))
                        effective = config.get("config") if isinstance(config, dict) else None
                        providers = effective.get("model_providers", {}) if isinstance(effective, dict) else None
                        if not isinstance(providers, dict):
                            raise ValueError("invalid provider configuration")
                    except Exception as exc:
                        raise ReconnectError("Could not verify the saved provider's effective project configuration; owner was not restarted.") from exc
                    providers_by_cwd[cwd] = providers
                definition = providers_by_cwd[cwd].get(provider)
                if not isinstance(definition, dict) or not definition:
                    raise ReconnectError("A saved conversation's model provider is absent from its current project configuration. Restore it or explicitly change that conversation's provider; owner was not restarted.")
            return account
        finally:
            rpc.close()

    def execute(self, plan):
        import fcntl
        if self.clock() - plan.created > 60:
            raise ReconnectError("Reconnect preview expired. Select Reconnect to inspect it again.")
        self.root.mkdir(parents=True, exist_ok=True)
        # Share the lock across dashboard windows and monitor state roots.
        lock_dir = Path.home() / ".cache/codex-monitor"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = lock_dir / (hashlib.sha256(plan.endpoint.encode()).hexdigest() + ".reconnect.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReconnectError("Another dashboard is reconnecting this owner.") from exc
            expected = self._fresh_account(plan)
            current = self.plan(plan.endpoint, plan.thread, policy=plan.policy)
            if (current.owner, current.residents, current.threads, current.pid) != (plan.owner, plan.residents, plan.threads, plan.pid):
                raise ReconnectError("Owner configuration or conversations changed. Inspect a new reconnect preview.")
            if plan.policy:
                from .permission_reconnect import execute_policy
                return execute_policy(self, plan, expected)
            try:
                self._launchctl("kickstart", "-k", self._domain(plan.owner))
            except ReconnectError:
                # launchctl can time out after launchd accepted the restart.
                # Observe the PID/account/threads; never issue another restart.
                pass
            deadline = self.clock() + 30
            restored = set()
            while self.clock() < deadline:
                rpc = None
                try:
                    if self._pid(plan.owner) != plan.pid:
                        rpc = self._rpc(plan.endpoint)
                        account = rpc.call("account/read", {"refreshToken": False}).get("account")
                        if account != expected:
                            raise ReconnectError("Owner restarted but its account differs from the verified current login.")
                        restored = set(plan.threads).intersection(self._threads(rpc))
                        if set(plan.threads).issubset(restored):
                            limits = rpc.call("account/rateLimits/read", {})
                            if not isinstance(limits, dict) or not any(isinstance(limits.get(k), dict)
                                                                      for k in ("rateLimits", "rateLimitsByLimitId")):
                                raise ReconnectError("Owner restarted but account access could not be verified.")
                            return (f"Reconnected with the current login; {len(plan.threads)} existing conversations restored. "
                                    "Open in Codex to continue failed work. No failed input was replayed.")
                except ReconnectError as exc:
                    if "account differs" in str(exc):
                        raise
                except Exception:
                    pass
                finally:
                    if rpc:
                        rpc.close()
                self.sleep(.25)
            raise ReconnectError(f"Reconnect verification timed out: {len(restored)}/{len(plan.threads)} conversations observed restored. "
                                 "Inspect the owner and resident services; do not blindly retry.")
        finally:
            os.close(fd)
