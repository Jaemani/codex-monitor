#!/usr/bin/env python3
"""Acceptance canary for installed, conversation-scoped managed monitors.

The default run exercises the installed wheel from outside this checkout.  It
starts the real receiver process and uses the repository's scripted App Server
only as a queue peer: a passing fake-peer run proves process, registry,
checkpoint and queue-placement behavior, never model or UI behavior.

``--tui`` is an opt-in extension for an ordinary Codex TUI in an owned PTY.
It is intentionally reported as a separate evidence surface and is never
enabled by the protocol canary.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fake_app_server.py"
MONITOR_NAME = "same-name"
FIFO_NAME = "blocked-fifo"


class CanaryError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_value(raw: str):
    """Decode a CLI response, tolerating a final blank line only."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CanaryError(f"CLI did not return JSON: {raw[:1000]!r}") from exc


def recursive_records(value):
    """Return dict records from either list or documented JSON envelopes."""

    if isinstance(value, dict):
        records = []
        if any(key in value for key in ("name", "thread", "file", "state", "enabled")):
            records.append(value)
        for item in value.values():
            records.extend(recursive_records(item))
        return records
    if isinstance(value, list):
        records = []
        for item in value:
            records.extend(recursive_records(item))
        return records
    return []


class Canary:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.report = {
            "result": "RUNNING",
            "scope": "installed wheel / real receiver / managed file producer",
            "evidence_kind": "fake App Server protocol and process evidence; no model or UI claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
            "checks": {},
            "steps": [],
            "started_at": now(),
            "python": str(args.python),
        }
        if args.wheel_sha256:
            self.report["wheel_sha256"] = args.wheel_sha256
        self.processes: list[subprocess.Popen] = []
        self.receiver: subprocess.Popen | None = None
        self.receiver_logs: list[object] = []
        self.fake_server: subprocess.Popen | None = None
        self.fake_bin: Path | None = None
        self.environment: dict[str, str] | None = None
        self.state: Path | None = None
        self.fake_state: Path | None = None
        self.port: int | None = None

    def save_report(self):
        path = self.args.report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    def step(self, name: str, **details):
        item = {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "at": now(), **details}
        self.report["steps"].append(item)
        self.save_report()
        print(json.dumps({"progress": item}), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def clean_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("CODEX_THREAD_ID", None)
        if self.fake_bin:
            env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        return env

    def cli(self, *argv: str, expected: int = 0, env: dict[str, str] | None = None,
            timeout: float = 20):
        if self.state is None:
            raise CanaryError("runtime state is not initialized")
        command = [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv]
        process = subprocess.run(
            command,
            cwd=self.work,
            env=env or self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if process.returncode != expected:
            raise CanaryError(
                f"CLI {argv!r} exited {process.returncode}, expected {expected}: "
                f"{process.stderr.strip()[:2000]}"
            )
        if not process.stdout.strip():
            return None
        return json_value(process.stdout)

    def monitor(self, action: str, *argv: str, thread: str | None = None,
                env_thread: str | None = None, expected: int = 0):
        args = ["monitor", action, *argv]
        if thread is not None:
            args.extend(["--thread", thread])
        env = dict(self.environment or {})
        if env_thread is not None:
            env["CODEX_THREAD_ID"] = env_thread
        return self.cli(*args, expected=expected, env=env)

    def read_fake(self) -> dict:
        if self.fake_state is None:
            raise CanaryError("fake state is not initialized")
        if not self.fake_state.exists():
            # The receiver must not start an App Server peer for a silent
            # initial sample.  Treat an absent peer state as an empty history.
            return {"history": [], "queued": []}
        for _ in range(20):
            try:
                value = json.loads(self.fake_state.read_text())
                if isinstance(value, dict):
                    return value
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.02)
        raise CanaryError("fake App Server state is unavailable or malformed")

    def history(self, thread: str) -> list[dict]:
        return [row for row in self.read_fake().get("history", []) if row.get("threadId") == thread]

    def history_digest_count(self, thread: str, value: str) -> int:
        return sum(value in json.dumps(row, ensure_ascii=False) for row in self.history(thread))

    def history_current_digest_count(self, thread: str, value: str) -> int:
        """Count a hash in the rendered event's current sample only.

        A later event legitimately repeats the prior hash in ``previous``;
        counting the whole rendered message would call that a duplicate.
        """

        count = 0
        for row in self.history(thread):
            text = "\n".join(
                part.get("text", "") for part in row.get("content", [])
                if isinstance(part, dict)
            )
            match = re.search(r"current:\s+.*?sha256:\s*([0-9a-f]{64})", text, re.DOTALL)
            if match and match.group(1) == value:
                count += 1
        return count

    def wait_for(self, condition, label: str, timeout: float | None = None):
        deadline = time.monotonic() + (timeout if timeout is not None else self.args.timeout)
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (OSError, urllib.error.URLError, CanaryError) as exc:
                last_error = exc
            time.sleep(.1)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def http(self, path: str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": "Bearer " + (self.admin_token if path == "/v1/status" else self.admin_token)},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def status_ready(self):
        try:
            value = self.http("/v1/status")
            return value if value.get("worker_error") is None else None
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def create_fake_codex(self):
        self.fake_bin = self.work / "fake-bin"
        self.fake_bin.mkdir()
        launcher = self.fake_bin / "codex"
        # The fake server is deliberately a protocol peer only.  It is never
        # presented as a model, a Codex UI, or a real client.
        launcher.write_text(
            "#!/bin/sh\n"
            + "exec " + _shell_quote(sys.executable) + " " + _shell_quote(str(FAKE_APP_SERVER))
            + " " + _shell_quote(str(self.fake_state)) + "\n"
        )
        launcher.chmod(0o755)

    def start_receiver(self):
        log_path = self.work / f"serve-{len(self.receiver_logs)}.log"
        log = log_path.open("w")
        self.receiver_logs.append(log)
        self.receiver = subprocess.Popen(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), "serve"],
            cwd=self.work,
            env=self.environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(self.receiver)
        self.wait_for(self.status_ready, "receiver readiness")
        if self.receiver.poll() is not None:
            raise CanaryError(f"receiver exited with {self.receiver.returncode}; see {log_path}")
        self.step("receiver_started", pid=self.receiver.pid, log=str(log_path))

    def stop_receiver(self, *, kill: bool = False):
        process = self.receiver
        if process is None or process.poll() is not None:
            self.receiver = None
            return
        if kill:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        process.wait(timeout=20)
        self.receiver = None

    def setup(self):
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-managed-", dir="/tmp"))
        self.state = self.work / "state"
        self.fake_state = self.work / "fake-app-server.json"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        # Ensure this process really uses the installed wheel, even when the
        # harness itself is invoked from the repository checkout.
        self.create_fake_codex()
        env = self.clean_environment()
        self.environment = env
        probe = subprocess.run(
            [str(self.args.python), "-c", "import codex_monitor; print(codex_monitor.__file__)"],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if probe.returncode:
            raise CanaryError(f"installed-runtime probe failed: {probe.stderr.strip()[:1000]}")
        package = Path(probe.stdout.strip()).resolve()
        if package.is_relative_to(ROOT):
            raise CanaryError(f"runtime imports checkout source, not an installed wheel: {package}")
        self.report["installed_package"] = str(package)
        self.report["fake_app_server"] = str(FAKE_APP_SERVER)
        self.report["process_model"] = {"receiver": "separate serve process", "fake_peer": "separate stdio process"}
        self.cli("init", "--port", str(self.port))
        self.admin_token = (self.state / "admin.token").read_text().strip()
        self.cli("status")
        self.step("installed_runtime_initialized", port=self.port, state=str(self.state))

    def run_protocol(self):
        file_a = self.work / "conversation-a.txt"
        file_b = self.work / "conversation-b.txt"
        self.managed_files = {"conversation_a": file_a, "conversation_b": file_b}
        file_a.write_text("baseline-a\n")
        file_b.write_text("baseline-b\n")
        self.cli("source", "managed")

        self.check("missing_thread_is_rejected", self.monitor("list", expected=2) is None)

        # A is selected explicitly; B proves the host-provided exact thread
        # environment is honored.  Both deliberately use the same local name.
        self.monitor("create", MONITOR_NAME, "--file", str(file_a), "--interval", "0.1", thread="thread-user")
        self.monitor("create", MONITOR_NAME, "--file", str(file_b), "--interval", "0.1", env_thread="thread-other")
        a_list = self.monitor("list", thread="thread-user")
        b_list = self.monitor("list", thread="thread-other")
        self.check("same_name_isolated_by_thread", json.dumps(a_list) != json.dumps(b_list))
        self.check("explicit_and_environment_thread_selection", "thread-user" in json.dumps(a_list) and "thread-other" in json.dumps(b_list))

        self.start_receiver()
        initial_a = digest(file_a)
        initial_b = digest(file_b)
        self.wait_for(
            lambda: (self.monitor("status", MONITOR_NAME, thread="thread-user").get("last_sample") or {}).get("sha256") == initial_a,
            "conversation A initial checkpoint",
        )
        self.wait_for(
            lambda: (self.monitor("status", MONITOR_NAME, thread="thread-other").get("last_sample") or {}).get("sha256") == initial_b,
            "conversation B initial checkpoint",
        )
        self.check("initial_sample_is_silent", not self.history("thread-user") and not self.history("thread-other"))

        file_a.write_text("change-a-1\n")
        hash_a1 = digest(file_a)
        self.wait_for(lambda: self.history_digest_count("thread-user", hash_a1) == 1, "conversation A first change")
        self.check("change_receipt_reaches_selected_thread", self.history_digest_count("thread-user", hash_a1) == 1)
        self.check("change_does_not_reach_other_thread", self.history_digest_count("thread-other", hash_a1) == 0)
        before_pause = len(self.history("thread-user"))

        self.monitor("pause", MONITOR_NAME, thread="thread-user")
        file_a.write_text("paused-a\n")
        hash_a_paused = digest(file_a)
        time.sleep(.8)
        self.check("pause_suppresses_new_delivery", self.history_digest_count("thread-user", hash_a_paused) == 0)

        file_b.write_text("change-b-while-a-paused\n")
        hash_b1 = digest(file_b)
        self.wait_for(lambda: self.history_digest_count("thread-other", hash_b1) == 1, "conversation B while A paused")
        self.check("paused_thread_does_not_block_other_thread", self.history_digest_count("thread-other", hash_b1) == 1)
        self.check("pause_does_not_rewrite_prior_history", len(self.history("thread-user")) == before_pause)

        self.monitor("resume", MONITOR_NAME, thread="thread-user")
        self.wait_for(lambda: self.history_digest_count("thread-user", hash_a_paused) == 1, "conversation A resume delivery")
        self.check("resume_delivers_retained_change_once", self.history_digest_count("thread-user", hash_a_paused) == 1)

        self.monitor("remove", MONITOR_NAME, thread="thread-user")
        file_a.write_text("removed-a\n")
        hash_a_removed = digest(file_a)
        file_b.write_text("change-b-after-a-removed\n")
        hash_b2 = digest(file_b)
        self.wait_for(lambda: self.history_digest_count("thread-other", hash_b2) == 1, "conversation B after A removal")
        time.sleep(.8)
        self.check("remove_is_scoped_to_one_thread", self.history_digest_count("thread-user", hash_a_removed) == 0)
        self.check("remaining_same_name_monitor_still_runs", self.history_digest_count("thread-other", hash_b2) == 1)

        # A receiver crash must retain the registry and checkpoints.  With no
        # source change, restart must remain quiet; one later change produces
        # one receipt, even though a fresh supervisor process is used.
        self.stop_receiver(kill=True)
        self.start_receiver()
        self.wait_for(
            lambda: (self.monitor("status", MONITOR_NAME, thread="thread-other").get("last_sample") or {}).get("sha256") == hash_b2,
            "conversation B checkpoint after restart",
        )
        self.check("sigkill_restart_preserves_checkpoint_without_duplicate", self.history_digest_count("thread-other", hash_b2) == 1)
        file_b.write_text("change-b-after-restart\n")
        hash_b3 = digest(file_b)
        self.wait_for(lambda: self.history_digest_count("thread-other", hash_b3) == 1, "conversation B after restart")
        time.sleep(.5)
        self.check("restart_change_is_delivered_once", self.history_digest_count("thread-other", hash_b3) == 1)

        # A FIFO is a non-regular file.  A safe managed producer must reject
        # or report it without opening it for blocking reads.  Keep B active as
        # the liveness witness while the FIFO monitor is registered.
        fifo = self.work / "blocked.fifo"
        os.mkfifo(fifo)
        self.monitor("create", FIFO_NAME, "--file", str(fifo), "--interval", "0.1", thread="thread-user")
        file_b.write_text("change-b-with-fifo-present\n")
        hash_b_fifo = digest(file_b)
        self.wait_for(lambda: self.history_digest_count("thread-other", hash_b_fifo) == 1, "healthy monitor beside FIFO")
        self.check("nonregular_file_does_not_hang_other_monitor", self.history_digest_count("thread-other", hash_b_fifo) == 1)
        self.check("receiver_remains_healthy_with_nonregular_file", self.status_ready() is not None)
        self.monitor("remove", FIFO_NAME, thread="thread-user")
        self.report["files"] = {"conversation_a": str(file_a), "conversation_b": str(file_b), "fifo": str(fifo)}

    def run_soak(self):
        """Keep the installed receiver alive while exercising real checkpoints."""

        requested = self.args.soak_seconds
        if requested <= 0:
            return
        file_b = self.managed_files["conversation_b"]
        started = time.monotonic()
        deadline = started + requested
        interval = max(1.0, min(10.0, requested / 4))
        next_change = started + min(2.0, interval)
        restart_at = started + max(3.0, requested / 2)
        event_count = 0
        restart_count = 0
        idle_samples = 0
        known_hashes = []
        initial_count = len(self.history("thread-other"))
        idle_until = min(deadline, started + min(2.0, max(.5, requested / 4)))
        while time.monotonic() < idle_until:
            if self.status_ready() is None:
                raise CanaryError("receiver became unhealthy during unchanged soak interval")
            if len(self.history("thread-other")) != initial_count:
                raise CanaryError("unchanged soak interval created a native history entry")
            idle_samples += 1
            time.sleep(.25)

        while time.monotonic() < deadline:
            current = time.monotonic()
            if current >= restart_at and restart_count == 0 and current < deadline:
                before_restart = len(self.history("thread-other"))
                self.stop_receiver(kill=True)
                self.start_receiver()
                self.wait_for(lambda: self.status_ready() is not None, "soak receiver restart")
                time.sleep(min(.5, max(.1, deadline - time.monotonic())))
                if len(self.history("thread-other")) != before_restart:
                    raise CanaryError("receiver restart duplicated a managed event during soak")
                restart_count += 1
                restart_at = deadline + 1
                self.step("soak_receiver_restarted", restart=restart_count)
                continue
            if current >= next_change and current < deadline:
                event_count += 1
                file_b.write_text(f"managed-soak-{event_count}-{time.monotonic_ns()}\n")
                value = digest(file_b)
                known_hashes.append(value)
                self.wait_for(
                    lambda value=value: self.history_digest_count("thread-other", value) == 1,
                    f"soak change {event_count}",
                )
                self.check(
                    "soak_change_has_one_receipt",
                    self.history_digest_count("thread-other", value) == 1,
                    event=event_count,
                )
                next_change += interval
                continue
            if self.status_ready() is None:
                raise CanaryError("receiver became unhealthy during soak")
            time.sleep(.25)
        if event_count == 0:
            raise CanaryError("soak did not reach a file-change checkpoint")
        self.report["soak"] = {
            "requested_seconds": requested,
            "actual_seconds": round(time.monotonic() - started, 3),
            "events": event_count,
            "restarts": restart_count,
            "unchanged_idle_samples": idle_samples,
            "duplicate_hashes": [value for value in known_hashes if self.history_current_digest_count("thread-other", value) != 1],
            "model_delivery_verified": False,
        }
        self.check("soak_checkpoint_events_are_unique", not self.report["soak"]["duplicate_hashes"])

    def run(self):
        self.setup()
        self.run_protocol()
        self.run_soak()
        if self.args.tui:
            self.run_tui()
            if self.report.get("tui", {}).get("result") != "PASS":
                self.report["result"] = "INCOMPLETE"
                self.step("managed_monitor_protocol_canary_incomplete", result="INCOMPLETE",
                           tui_result=self.report.get("tui", {}).get("result"))
                return
        self.report["result"] = "PASS"
        self.step("managed_monitor_protocol_canary_completed", result="PASS")

    def run_tui(self):
        """Run an isolated ordinary Codex TUI and inspect its rendered event.

        This path deliberately has a separate state directory and environment
        from the fake protocol peer.  It creates a normal ``codex`` process in
        the PTY helper's owned child, then uses only the installed receiver for
        the managed producer.  A rendered hash proves TUI visibility; it does
        not claim model completion.
        """

        tui_report = {
            "result": "FAIL",
            "surface": "ordinary Codex TUI in owned disposable PTY",
            "evidence_kind": "pyte-rendered terminal buffer from the owned PTY; no model completion claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
        }
        terminal = tui_receiver = rpc = remote_server = None
        receiver_log = None
        try:
            import importlib.util

            helper_spec = importlib.util.spec_from_file_location("managed_tui_helpers", ROOT / "scripts" / "tui-canary.py")
            if helper_spec is None or helper_spec.loader is None:
                raise CanaryError("cannot load owned PTY helper")
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
            Terminal = helper.Terminal
            RemoteServer = helper.RemoteServer
            from codex_monitor.session import Rpc

            work = Path(tempfile.mkdtemp(prefix="codex-monitor-tui-", dir="/tmp"))
            state = work / "state"
            file_path = work / "watched.txt"
            file_path.write_text("tui-baseline\n")
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("CODEX_THREAD_ID", None)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]

            def tui_cli(*argv):
                process = subprocess.run(
                    [str(self.args.python), "-m", "codex_monitor", "--state", str(state), *argv],
                    cwd=work, env=env, capture_output=True, text=True, timeout=30,
                )
                if process.returncode:
                    raise CanaryError(f"TUI CLI {argv!r} failed: {process.stderr.strip()[:1200]}")
                return json_value(process.stdout) if process.stdout.strip() else None

            tui_cli("init", "--port", str(port))
            admin = (state / "admin.token").read_text().strip()

            endpoint = "shared-local"

            def ready():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/status",
                    headers={"Authorization": "Bearer " + admin},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    value = json.load(response)
                return value if value.get("worker_error") is None else None

            instruction = (
                "This is an isolated owned-PTY acceptance test. Do not use tools or read files. "
                "Reply exactly READY_TUI_MANAGED to this initial prompt. For the later user marker "
                "TUI_FOLLOWUP, reply exactly ACK_TUI_FOLLOWUP."
            )
            tui_argv = [
                "codex", "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never",
                "-c", f"model_reasoning_effort=\"{self.args.reasoning_effort}\"",
                "-c", "tui.animations=false", instruction,
            ]
            if self.args.remote:
                remote_server = RemoteServer(work / "app-server.sock")
                endpoint = remote_server.endpoint
                tui_argv[1:1] = ["--remote", endpoint]
                tui_report.update(surface="ordinary Codex TUI through owned Unix App Server",
                                   endpoint=endpoint)
            if self.args.model:
                tui_argv[1:1] = ["--model", self.args.model]
            terminal = Terminal(tui_argv)
            rpc = Rpc(endpoint)
            tui_report["process_model"] = {"remote_app_server_pid": remote_server.pid} if remote_server else {}
            trust_sent = False

            def pump_until(condition, label, timeout=None):
                nonlocal trust_sent
                deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
                while time.monotonic() < deadline:
                    terminal.pump(.1)
                    screen = terminal.text()
                    if not trust_sent and ("Yes, I trust" in screen or "Yes, continue" in screen):
                        terminal.input("\r")
                        trust_sent = True
                    if condition():
                        return
                raise CanaryError(f"{label} timed out")

            thread = None

            def turns():
                return rpc.call("thread/turns/list", {
                    "threadId": thread, "itemsView": "full", "limit": 100,
                })["data"] if thread else []

            def native_has(kind, marker):
                for turn in turns():
                    for item in turn.get("items", []):
                        if item.get("type") != kind:
                            continue
                        if kind == "agentMessage":
                            text = item.get("text", "")
                        else:
                            text = json.dumps(item.get("content", []), ensure_ascii=False)
                        if marker in text:
                            return True
                return False

            def find_thread():
                nonlocal thread
                list_params = {"cwd": str(work), "limit": 10}
                if not self.args.remote:
                    list_params["sourceKinds"] = ["cli"]
                rows = rpc.call("thread/list", list_params)["data"]
                if len(rows) == 1:
                    thread = rows[0]["id"]
                return thread

            pump_until(find_thread, "ordinary TUI thread discovery")
            pump_until(lambda: native_has("agentMessage", "READY_TUI_MANAGED"),
                       "initial native TUI response")
            created = tui_cli("monitor", "create", "tui-watch", "--thread", thread,
                              "--file", str(file_path), "--interval", "0.2",
                              "--endpoint", endpoint)
            if not isinstance(created, dict) or created.get("endpoint") != endpoint:
                raise CanaryError(f"managed monitor did not persist explicit endpoint {endpoint!r}: {created!r}")
            tui_report["persisted_endpoint"] = endpoint
            receiver_log = (work / "serve.log").open("w")
            tui_receiver = subprocess.Popen(
                [str(self.args.python), "-m", "codex_monitor", "--state", str(state), "serve"],
                cwd=work, env=env, stdout=receiver_log, stderr=subprocess.STDOUT, start_new_session=True,
            )

            def receiver_ready():
                try:
                    return bool(ready())
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    return False

            pump_until(receiver_ready, "TUI receiver readiness", timeout=30)
            loaded = rpc.call("thread/loaded/list", {})["data"]
            self.check(
                "managed_tui_endpoint_server_has_exact_loaded_thread",
                loaded == [thread] or thread in loaded,
                endpoint=endpoint, thread=thread, loaded_thread_ids=loaded,
            )
            if self.args.remote:
                self.report["checks"]["managed_tui_endpoint_server_has_exact_loaded_thread"] = True
            initial_hash = digest(file_path)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    status = tui_cli("monitor", "status", "tui-watch", "--thread", thread)
                    if (status.get("last_sample") or {}).get("sha256") == initial_hash:
                        break
                except (OSError, CanaryError):
                    pass
                terminal.pump(.1)
            else:
                raise CanaryError("TUI managed collector did not checkpoint its silent baseline")
            change_at = time.monotonic()
            file_path.write_text("tui-managed-change\n")
            marker = digest(file_path)
            local_intake_at = None
            accepted_at = None
            consumed_at = None
            visible_at = None
            delivery_id = None
            timing_deadline = time.monotonic() + 30

            def tui_http(path):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Authorization": "Bearer " + admin},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    return json.load(response)

            while time.monotonic() < timing_deadline:
                terminal.pump(.1)
                status = tui_cli("monitor", "status", "tui-watch", "--thread", thread)
                if local_intake_at is None and (status.get("last_sample") or {}).get("sha256") == marker:
                    local_intake_at = time.monotonic()
                last_delivery = status.get("last_delivery") or {}
                if delivery_id is None and last_delivery.get("delivery_id"):
                    delivery_id = last_delivery["delivery_id"]
                if delivery_id and accepted_at is None:
                    delivery = tui_http("/v1/deliveries/" + delivery_id)
                    if delivery.get("state") == "accepted":
                        accepted_at = time.monotonic()
                if consumed_at is None and native_has("userMessage", marker):
                    consumed_at = time.monotonic()
                if visible_at is None and marker in terminal.text():
                    visible_at = time.monotonic()
                if local_intake_at and accepted_at and consumed_at and visible_at:
                    break
            if not all((local_intake_at, accepted_at, consumed_at, visible_at, delivery_id)):
                raise CanaryError(
                    "managed TUI timing sample incomplete: "
                    f"local={local_intake_at!r} accepted={accepted_at!r} "
                    f"consumed={consumed_at!r} visible={visible_at!r} delivery={delivery_id!r}"
                )
            delivery = tui_http("/v1/deliveries/" + delivery_id)
            final_status = tui_cli("monitor", "status", "tui-watch", "--thread", thread)
            if final_status.get("thread") != thread or final_status.get("endpoint") != endpoint:
                raise CanaryError(
                    "managed delivery status lost the exact target binding: "
                    f"thread={final_status.get('thread')!r} endpoint={final_status.get('endpoint')!r}"
                )
            timing = {
                "change_to_local_intake_seconds": round(local_intake_at - change_at, 6),
                "local_intake_to_native_accepted_seconds": round(accepted_at - local_intake_at, 6),
                "native_accepted_to_consumed_seconds": round(consumed_at - accepted_at, 6),
                "consumed_to_visible_seconds": round(visible_at - consumed_at, 6),
                "change_to_visible_seconds": round(visible_at - change_at, 6),
                "delivery_id": delivery_id,
                "local_intake_at": local_intake_at,
                "native_accepted_at": accepted_at,
                "native_consumed_at": consumed_at,
                "visible_at": visible_at,
            }
            tui_report["sample_timing"] = timing
            self.report["checks"]["managed_tui_change_intake_acceptance_consumption_visibility_timed_separately"] = True
            self.step("managed_tui_change_timing_sampled", **timing)
            pump_until(
                lambda: "esc to interrupt" not in terminal.text().lower(),
                "ordinary TUI idle after managed event",
            )

            # Once the event is visibly consumed, send an ordinary user turn
            # and require its model response in native history. This separates
            # same-conversation event placement from model completion evidence.
            terminal.input("marker TUI_FOLLOWUP")
            for _ in range(5):
                terminal.pump(.1)
            terminal.input("\r")
            pump_until(lambda: native_has("agentMessage", "ACK_TUI_FOLLOWUP"),
                       "ordinary TUI follow-up response")
            tui_report.update(
                result="PASS", client_ui_verified=True, rendered_hash=marker, thread=thread,
                event_consumed=True, followup_response_seen=True,
                process_model={"tui_pid": terminal.pid, "receiver_pid": tui_receiver.pid,
                               **({"remote_app_server_pid": remote_server.pid} if remote_server else {})},
            )
            self.report["checks"]["ordinary_tui_renders_managed_watch_event"] = True
            self.step("ordinary_tui_managed_event_rendered", thread=thread, rendered_hash=marker)
        except ModuleNotFoundError as exc:
            if exc.name == "pyte":
                tui_report.update(result="SKIPPED", reason="optional PTY dependency pyte is not installed")
                self.step("ordinary_tui_skipped", reason=tui_report["reason"])
                return
            tui_report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        except FileNotFoundError as exc:
            if exc.filename == "codex":
                tui_report.update(result="SKIPPED", reason="codex executable is not available on PATH")
                self.step("ordinary_tui_skipped", reason=tui_report["reason"])
                return
            tui_report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        except Exception as exc:
            tui_report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if terminal is not None:
                buffer_path = self.args.report.with_suffix(".tui-terminal.txt")
                try:
                    buffer_path.write_text(terminal.text())
                    tui_report["terminal_buffer_capture"] = str(buffer_path)
                except OSError as exc:
                    tui_report["terminal_buffer_capture_error"] = str(exc)
            if terminal is not None:
                terminal.close()
            if tui_receiver is not None and tui_receiver.poll() is None:
                try:
                    os.killpg(tui_receiver.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    tui_receiver.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(tui_receiver.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    tui_receiver.wait(timeout=5)
            if receiver_log is not None:
                receiver_log.close()
            if rpc is not None:
                rpc.close()
            if remote_server is not None:
                remote_server.close()
            self.report["tui"] = tui_report

    def cleanup(self):
        self.stop_receiver()
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for stream in self.receiver_logs:
            try:
                stream.close()
            except OSError:
                pass
        if getattr(self, "work", None):
            self.report["temporary_work"] = str(self.work)
            shutil.rmtree(self.work, ignore_errors=True)
            self.report["temporary_work_removed"] = True

    def finish(self):
        self.report["finished_at"] = now()
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.save_report()
        if self.report.get("result") == "FAIL":
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            preserved = self.args.report.with_name(self.args.report.stem + ".failure-" + suffix + self.args.report.suffix)
            preserved.write_text(self.args.report.read_text())
            self.report["preserved_failure_report"] = str(preserved)
            self.save_report()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def _shell_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--python", required=True, type=Path, help="absolute Python from the clean installed wheel")
    parser.add_argument("--wheel-sha256", help="SHA-256 of the wheel installed into --python, for evidence correlation")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--soak-seconds", type=int, default=0,
                        help="optional real elapsed managed-collector soak duration")
    parser.add_argument("--tui", action="store_true", help="also run the separate opt-in ordinary Codex TUI canary")
    parser.add_argument("--remote", action="store_true",
                        help="run the TUI phase through an owned Unix App Server endpoint")
    parser.add_argument("--model", help="optional model override for the owned ordinary TUI")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        default="low",
        help="reasoning effort for the owned ordinary TUI (default: low)",
    )
    args = parser.parse_args()
    if not args.python.is_absolute() or not args.python.is_file() or not os.access(args.python, os.X_OK):
        parser.error("--python must be an absolute executable installed-runtime Python")
    if args.timeout < 5:
        parser.error("--timeout must be at least 5 seconds")
    if args.soak_seconds < 0:
        parser.error("--soak-seconds must be non-negative")
    if args.remote and not args.tui:
        parser.error("--remote requires --tui")
    if args.wheel_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.wheel_sha256):
        parser.error("--wheel-sha256 must be a 64-character hexadecimal digest")
    canary = Canary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            canary.step("managed_monitor_protocol_canary_failed", error=canary.report["error"])
        except Exception:
            pass
    finally:
        canary.cleanup()
        canary.finish()
    return 0 if canary.report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
