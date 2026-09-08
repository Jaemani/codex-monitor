#!/usr/bin/env python3
"""Exercise the installed codex-monitor skill through an ordinary Codex TUI.

The canary owns the CLI process, its PTY, a disposable workspace, the monitor
state directory, and the receiver process.  The monitor is configured only by
natural-language turns that explicitly invoke ``$codex-monitor``.  Direct
CLI/HTTP and App Server reads are used for evidence; assistant prose is used
only to synchronize turns and is never treated as proof of delivery.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import urllib.error
import urllib.request

import pyte

from codex_monitor.session import Rpc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MONITOR_BIN = Path.home() / ".local/share/codex-monitor/bin/codex-monitor"
DEFAULT_SKILL = Path.home() / ".codex/skills/codex-monitor/scripts/monitor.py"
DEFAULT_PYTHON = Path(sys.executable).resolve()
MODEL = "gpt-5.6-luna"
REASONING = "xhigh"
WATCH_NAME = "workflow-canary"


class CanaryError(RuntimeError):
    pass


class Terminal:
    """A PTY owned by this process with a deterministic pyte terminal view."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path):
        self.screen = pyte.HistoryScreen(120, 45, history=2000)
        self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.environ.update(env)
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("NO_COLOR", None)
            os.execvp(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
        self.raw = bytearray()

    def pump(self, duration: float = 0.1):
        if select.select([self.fd], [], [], duration)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            self.raw.extend(data)
            self.stream.feed(data)
            if b"\x1b[6n" in data:
                os.write(self.fd, b"\x1b[1;1R")
            if b"\x1b[c" in data:
                os.write(self.fd, b"\x1b[?1;2c")

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def input(self, value: str):
        os.write(self.fd, value.encode())

    def close(self):
        try:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                os.kill(self.pid, signal.SIGTERM)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    break
                self.pump()
            else:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report = {
            "result": "RUNNING",
            "workflow": "natural-language installed codex-monitor skill",
            "surface": "ordinary Codex CLI TUI in an owned disposable PTY",
            "evidence_kind": (
                "pyte terminal capture plus installed CLI/HTTP and App Server history; "
                "assistant claims are synchronization only"
            ),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "checks": {},
            "steps": [],
            "prompts": [],
            "started_at": utc_now(),
            "process_model": {"harness_pid": os.getpid()},
        }
        self.terminal: Terminal | None = None
        self.receiver: subprocess.Popen | None = None
        self.receiver_log = None
        self.rpc: Rpc | None = None
        self.thread: str | None = None
        self.work: Path | None = None
        self.state: Path | None = None
        self.file_path: Path | None = None
        self.env: dict[str, str] | None = None
        self.port: int | None = None
        self.admin_token: str | None = None
        self.buffers: dict[str, str] = {}
        self.trust_sent = False

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.report["updated_at"] = utc_now()
        temporary = self.args.report.with_suffix(self.args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(self.args.report)

    def step(self, name: str, **details):
        value = {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                 "at": utc_now(), **details}
        self.report["steps"].append(value)
        self.save()
        print(json.dumps({"progress": value}, ensure_ascii=False), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def capture(self, name: str):
        if not self.terminal:
            return
        self.terminal.pump(0)
        self.buffers[name] = self.terminal.text()
        path = self.args.report.with_suffix(".terminal.txt")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n\n".join(
            f"=== {key} ===\n{value}" for key, value in self.buffers.items()
        ))
        temporary.replace(path)
        self.report["terminal_buffer_capture"] = str(path)

    def clean_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Keep the user's authenticated CODEX_HOME.  These are the only
        # monitor-specific overrides and point into this canary's workspace.
        env["CODEX_MONITOR_BIN"] = str(self.args.monitor_bin)
        env["CODEX_MONITOR_HOME"] = str(self.state)
        env.pop("CODEX_THREAD_ID", None)
        return env

    def helper(self, *argv: str, expected: int = 0, timeout: float = 30):
        if not self.work or not self.env:
            raise CanaryError("canary environment is not initialized")
        command = [str(self.args.python), str(self.args.skill_helper), *argv]
        process = subprocess.run(
            command, cwd=self.work, env=self.env, capture_output=True, text=True, timeout=timeout,
        )
        if process.returncode != expected:
            raise CanaryError(
                f"installed helper {argv!r} exited {process.returncode}, expected {expected}: "
                f"{process.stderr.strip()[:2000]}"
            )
        if not process.stdout.strip():
            return None
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CanaryError(f"helper did not return JSON for {argv!r}: {process.stdout[:1000]!r}") from exc

    def status(self):
        value = self.helper("monitor", "status", WATCH_NAME, "--thread", self.thread or "")
        if not isinstance(value, dict):
            raise CanaryError(f"unexpected monitor status: {value!r}")
        return value

    def event(self, delivery_id: str):
        value = self.helper("event", delivery_id)
        if not isinstance(value, dict):
            raise CanaryError(f"unexpected event response: {value!r}")
        return value

    def http(self, path: str):
        if not self.port or not self.admin_token:
            raise CanaryError("receiver HTTP credentials are unavailable")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": "Bearer " + self.admin_token},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def wait(self, condition, label: str, timeout: float | None = None):
        deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
        last_error = None
        while time.monotonic() < deadline:
            # Keep the owned TUI flowing while polling native history or
            # receiver state. Approval prompts are rejected by pump_once;
            # only a narrowly identified disposable-folder trust modal may
            # be answered there.
            if self.terminal:
                self.pump_once(.1)
            try:
                result = condition()
                if result:
                    return result
            except (OSError, urllib.error.URLError, json.JSONDecodeError, CanaryError) as exc:
                last_error = exc
            time.sleep(.1)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def turns(self):
        if not self.rpc or not self.thread:
            return []
        return self.rpc.call("thread/turns/list", {
            "threadId": self.thread, "itemsView": "full", "limit": 100,
        })["data"]

    def native_client_count(self, client_id: str) -> int:
        return sum(
            item.get("clientId") == client_id
            for turn in self.turns() for item in turn.get("items", [])
            if item.get("type") == "userMessage"
        )

    def native_agent_marker(self, marker: str) -> bool:
        return any(
            turn.get("status") == "completed"
            and any(item.get("type") == "agentMessage" and marker in item.get("text", "")
                    for item in turn.get("items", []))
            for turn in self.turns()
        )

    def screen_idle(self) -> bool:
        if not self.terminal:
            return False
        screen = self.terminal.text().lower()
        return "ask codex to do anything" in screen and "esc to interrupt" not in screen

    def approval_visible(self, screen: str) -> bool:
        lower = screen.lower()
        return (
            "would you like to run the following command" in lower
            or "yes, proceed" in lower
            or "allow command" in lower
        )

    def pump_once(self, duration: float = .1):
        if not self.terminal:
            return
        self.terminal.pump(duration)
        screen = self.terminal.text()
        # Classify command approvals first: their UI can also contain a
        # ``Yes, continue`` label. Only answer an explicitly identified initial
        # folder trust modal for this disposable workspace.
        if self.approval_visible(screen):
            self.capture("approval_required")
            raise CanaryError(
                "TUI requested command approval; canary does not auto-approve "
                "on-request commands"
            )
        lower = screen.lower()
        trusted_workspace = str(self.work).lower() in lower if self.work else False
        trust_modal = (
            "trust this folder" in lower
            or "trust the contents" in lower
            or "trust this workspace" in lower
        )
        if not self.trust_sent and trust_modal and trusted_workspace and (
            "yes, i trust" in lower or "yes, continue" in lower
        ):
            self.terminal.input("\r")
            self.trust_sent = True

    def inspect_consumed(self, delivery_id: str):
        value = self.helper("inspect", delivery_id)
        return value if value.get("native", {}).get("state") == "consumed" else None

    def wait_turn(self, marker: str, label: str):
        self.wait(lambda: self.native_agent_marker(marker), label + " native completion")
        self.wait(lambda: self.screen_idle(), label + " TUI idle")
        self.capture(label)
        self.check(label + " completed in native history", self.native_agent_marker(marker))

    def submit(self, prompt: str, marker: str):
        if not self.terminal:
            raise CanaryError("TUI is not running")
        self.wait(lambda: self.screen_idle(), "TUI ready before prompt")
        self.report["prompts"].append({"marker": marker, "text": prompt})
        self.terminal.input(prompt)
        for _ in range(5):
            self.pump_once(.05)
        self.terminal.input("\r")
        self.step("natural_language_turn_submitted", marker=marker, prompt=prompt)
        self.wait_turn(marker, marker)

    def start_receiver(self):
        if not self.work or not self.state or not self.env:
            raise CanaryError("receiver environment is not initialized")
        log_path = self.work / "receiver.log"
        self.receiver_log = log_path.open("w")
        self.receiver = subprocess.Popen(
            [str(self.args.monitor_bin), "--state", str(self.state), "serve"],
            cwd=self.work, env=self.env, stdout=self.receiver_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.report["process_model"]["receiver_pid"] = self.receiver.pid
        try:
            self.wait(lambda: self.http("/v1/status"), "authenticated receiver readiness", timeout=30)
        except CanaryError as exc:
            exit_code = self.receiver.poll()
            try:
                log_text = log_path.read_text()
            except OSError as log_exc:
                log_text = f"unable to read receiver log: {log_exc}"
            self.report["receiver_failure"] = {
                "exit_code": exit_code,
                "log": log_text[-10000:],
                "path": str(log_path),
            }
            raise CanaryError(
                f"receiver readiness failed (exit={exit_code}): {log_text[-2000:].strip()}"
            ) from exc
        self.step("receiver_started_and_authenticated", pid=self.receiver.pid, log=str(log_path))

    def stop_receiver(self):
        if self.receiver is not None and self.receiver.poll() is None:
            try:
                os.killpg(self.receiver.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.receiver.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.receiver.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.receiver.wait(timeout=5)
        self.receiver = None
        if self.receiver_log:
            self.receiver_log.close()
            self.receiver_log = None

    def setup(self):
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-skill-workflow-", dir="/tmp"))
        self.state = self.work / "monitor-state"
        self.file_path = self.work / "watched.txt"
        self.file_path.write_text("baseline\n")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.env = self.clean_env()
        version = subprocess.run(
            [str(self.args.monitor_bin), "--version"],
            cwd=self.work, env=self.env, capture_output=True, text=True, check=False, timeout=20,
        )
        self.report["runtime"] = {
            "binary": str(self.args.monitor_bin),
            "version": version.stdout.strip(),
            "binary_sha256": sha256(self.args.monitor_bin),
            "skill_helper": str(self.args.skill_helper),
            "skill_helper_sha256": sha256(self.args.skill_helper),
        }
        self.report["process_model"].update({
            "workspace": str(self.work), "state": str(self.state),
            "receiver": "separate installed serve process",
            "tui": "separate owned codex process in PTY",
        })
        self.step("disposable_workspace_created", workspace=str(self.work), state=str(self.state),
                   watched_file=str(self.file_path), port=self.port)

    def discover_thread_and_start_tui(self):
        if not self.work or not self.env:
            raise CanaryError("workspace is not initialized")
        instruction = (
            "This is an isolated readiness turn for an acceptance canary. Do not use tools, read files, "
            "or contact services. Reply exactly CANARY_READY."
        )
        argv = [
            "codex", "--model", self.args.model, "-C", str(self.work), "--no-alt-screen",
            "-s", "workspace-write", "-a", "on-request",
            "-c", f'model_reasoning_effort="{self.args.reasoning_effort}"',
            "-c", "tui.animations=false", instruction,
        ]
        self.terminal = Terminal(argv, self.env, self.work)
        self.report["process_model"]["tui_pid"] = self.terminal.pid
        self.rpc = Rpc("shared-local")

        def find_thread():
            params = {"cwd": str(self.work), "limit": 10, "sourceKinds": ["cli"]}
            rows = self.rpc.call("thread/list", params)["data"]
            if len(rows) == 1:
                self.thread = rows[0]["id"]
                return True
            if len(rows) > 1:
                raise CanaryError(f"expected one owned CLI thread, found {len(rows)}")
            return False

        self.wait(find_thread, "exact owned TUI thread discovery", timeout=90)
        self.wait(lambda: self.native_agent_marker("CANARY_READY"),
                  "readiness response in native history", timeout=180)
        self.wait(lambda: self.screen_idle(), "readiness TUI idle", timeout=60)
        self.capture("readiness")
        self.report["thread"] = self.thread
        self.step("exact_owned_thread_discovered", thread=self.thread)
        self.check("readiness_turn_completed", self.native_agent_marker("CANARY_READY"))

    def workflow_prompt(self, action: str, marker: str, extra: str = ""):
        assert self.thread and self.state and self.file_path
        prompt = (
            f"Invoke the $codex-monitor skill for this acceptance test. Work only on the exact current "
            f"conversation thread ID {self.thread}. Use the installed skill helper and its runtime, with "
            f"CODEX_MONITOR_HOME={self.state}; do not use the repository source implementation. The only "
            f"test file is {self.file_path}. {action} Use the exact thread ID on every monitor command. "
            f"Do not change application source code, credentials, or any file outside the disposable "
            f"workspace. {extra} When all requested commands finish, include {marker} in your final response."
        )
        self.submit(prompt, marker)

    def ensure_initialized(self):
        context = self.helper("context")
        self.check("installed_skill_runtime_resolved", context.get("executable") == str(self.args.monitor_bin),
                   context=context)
        self.check("skill_state_is_disposable", context.get("state") == str(self.state), context=context)

    def run_workflow(self):
        assert self.thread and self.state and self.file_path
        self.workflow_prompt(
            f"Initialize the disposable state, run the required doctor check for that exact thread, and "
            f"initialize it on receiver port {self.port}, and create managed monitor {WATCH_NAME!r} "
            f"for {self.file_path} with a 0.2 second interval. "
            "Do not start the receiver; the canary will start it after this turn.",
            "CANARY_CREATE_DONE",
        )
        created = self.helper("monitor", "status", WATCH_NAME, "--thread", self.thread)
        self.report["created_status"] = created
        # The installed CLI persists ``os.path.abspath``.  On macOS
        # ``Path.resolve`` canonicalizes /tmp to /private/tmp, which would
        # falsely report the exact same file as a mismatch.
        expected_file = os.path.abspath(str(self.file_path))
        created_matches = all((
            created.get("name") == WATCH_NAME,
            created.get("thread") == self.thread,
            created.get("file") == expected_file,
            bool(created.get("enabled")),
        ))
        self.report["created_binding_comparison"] = {
            "name": [created.get("name"), WATCH_NAME],
            "thread": [created.get("thread"), self.thread],
            "file": [created.get("file"), expected_file],
            "enabled": [created.get("enabled"), True],
            "matches": created_matches,
        }
        self.check("natural_language_create_persisted_exact_binding", created_matches,
                   status=created, expected_file=expected_file,
                   expected_thread=self.thread)
        self.step("managed_monitor_created_by_skill_workflow", monitor=WATCH_NAME)

        config = json.loads((self.state / "config.json").read_text())
        configured_port = config.get("port")
        if type(configured_port) is not int or not 1 <= configured_port <= 65535:
            raise CanaryError(f"initialized monitor config has invalid receiver port: {configured_port!r}")
        self.report["receiver_port"] = {
            "harness_requested": self.port,
            "configured_by_skill": configured_port,
        }
        # The skill's documented init flow uses its default port when the
        # model omits --port.  Verify and use the persisted value rather than
        # assuming the harness's reservation was selected.
        self.port = configured_port
        self.admin_token = (self.state / "admin.token").read_text().strip()
        self.start_receiver()
        self.wait(lambda: self.status().get("collector_status") == "running",
                  "collector running", timeout=45)
        baseline = sha256(self.file_path)
        self.wait(lambda: (self.status().get("last_sample") or {}).get("sha256") == baseline,
                  "initial baseline checkpoint", timeout=45)
        self.report["checks"]["receiver_registry_and_initial_checkpoint"] = True
        self.step("receiver_registry_and_initial_checkpoint_verified", baseline_sha256=baseline)

        # Active monitor: a changed file must produce one accepted delivery and
        # one native user message identified by that delivery's stable client ID.
        self.file_path.write_text("change-after-create\n")
        first_hash = sha256(self.file_path)
        first_status = self.wait(
            lambda: self.status() if (self.status().get("last_sample") or {}).get("sha256") == first_hash
            and self.status().get("last_delivery") else None,
            "post-create managed event intake", timeout=60,
        )
        first_delivery_id = first_status["last_delivery"]["delivery_id"]
        first_event = self.event(first_delivery_id)
        first_client = first_event["client_id"]
        self.wait(lambda: self.http("/v1/deliveries/" + first_delivery_id).get("state") == "accepted",
                  "post-create delivery accepted", timeout=30)
        self.wait(lambda: self.native_client_count(first_client) == 1,
                  "post-create native history entry", timeout=90)
        inspection = self.wait(lambda: self.inspect_consumed(first_delivery_id),
                               "post-create native consumed state", timeout=30)
        self.check("active_change_has_stable_native_history_entry",
                   inspection.get("local", {}).get("client_id") == first_client
                   and inspection.get("native", {}).get("state") == "consumed",
                   inspection=inspection)
        self.report["checks"]["active_change_receipt_accepted"] = True
        self.report["active_change"] = {"sha256": first_hash, "delivery_id": first_delivery_id,
                                         "client_id": first_client, "inspection": inspection}
        self.step("active_change_accepted_and_found_in_native_history",
                   delivery_id=first_delivery_id, client_id=first_client, sha256=first_hash)
        self.wait(lambda: self.screen_idle(), "TUI idle after active managed event", timeout=180)

        self.workflow_prompt(
            f"Inspect and report the managed monitor {WATCH_NAME!r} status and list for the exact thread. "
            "Report its enabled state, collector state, file, and last delivery. Do not change it.",
            "CANARY_STATUS_DONE",
        )
        inspected_status = self.status()
        self.check("natural_language_status_reports_exact_monitor",
                   inspected_status.get("name") == WATCH_NAME
                   and inspected_status.get("thread") == self.thread,
                   status=inspected_status)
        self.report["status_after_inspect"] = inspected_status

        self.workflow_prompt(
            f"Pause managed monitor {WATCH_NAME!r} for the exact thread. Verify and report that it is "
            "disabled/stopped. Do not remove it.",
            "CANARY_PAUSE_DONE",
        )
        paused = self.status()
        self.check("natural_language_pause_disabled_exact_monitor",
                   paused.get("enabled") is False and paused.get("collector_status") == "stopped",
                   status=paused)
        paused_delivery = (paused.get("last_delivery") or {}).get("delivery_id")
        paused_sample = (paused.get("last_sample") or {}).get("sha256")
        self.report["paused_status"] = paused

        self.file_path.write_text("change-while-paused\n")
        paused_hash = sha256(self.file_path)
        time.sleep(max(1.0, self.args.pause_observation_seconds))
        paused_after_change = self.status()
        self.check("paused_change_created_no_new_receipt_or_checkpoint",
                   (paused_after_change.get("last_delivery") or {}).get("delivery_id") == paused_delivery
                   and (paused_after_change.get("last_sample") or {}).get("sha256") == paused_sample,
                   status=paused_after_change)
        self.check("paused_change_created_no_native_history_entry",
                   self.native_client_count(first_client) == 1)
        self.report["paused_change"] = {"sha256": paused_hash, "delivery_id": paused_delivery}
        self.step("paused_change_remained_undelivered", sha256=paused_hash)

        self.workflow_prompt(
            f"Resume managed monitor {WATCH_NAME!r} for the exact thread. Verify and report that it is "
            "enabled/running. Do not recreate it.",
            "CANARY_RESUME_DONE",
        )
        resumed = self.status()
        self.check("natural_language_resume_enabled_exact_monitor", resumed.get("enabled") is True,
                   status=resumed)
        self.wait(lambda: self.status().get("last_delivery", {}).get("delivery_id") not in
                  (None, paused_delivery), "resumed change delivery", timeout=60)
        resumed_status = self.status()
        resumed_delivery_id = resumed_status["last_delivery"]["delivery_id"]
        resumed_event = self.event(resumed_delivery_id)
        resumed_client = resumed_event["client_id"]
        self.wait(lambda: self.http("/v1/deliveries/" + resumed_delivery_id).get("state") == "accepted",
                  "resumed delivery accepted", timeout=30)
        self.wait(lambda: self.native_client_count(resumed_client) == 1,
                  "resumed native history entry", timeout=90)
        resumed_inspection = self.wait(lambda: self.inspect_consumed(resumed_delivery_id),
                                       "resumed native consumed state", timeout=30)
        self.check("resumed_change_has_stable_native_history_entry",
                   resumed_inspection.get("local", {}).get("client_id") == resumed_client
                   and resumed_inspection.get("native", {}).get("state") == "consumed",
                   inspection=resumed_inspection)
        self.report["resumed_change"] = {"sha256": paused_hash, "delivery_id": resumed_delivery_id,
                                          "client_id": resumed_client, "inspection": resumed_inspection}
        self.step("resumed_change_accepted_and_found_in_native_history",
                   delivery_id=resumed_delivery_id, client_id=resumed_client)
        self.wait(lambda: self.screen_idle(), "TUI idle after resumed managed event", timeout=180)

        self.workflow_prompt(
            f"Remove managed monitor {WATCH_NAME!r} for the exact thread. Verify and report that the "
            "monitor is gone. Do not touch any other conversation or monitor.",
            "CANARY_REMOVE_DONE",
        )
        before_removed_events = set((self.http("/v1/status").get("events") or {}).keys())
        removed = self.helper("monitor", "list", "--thread", self.thread)
        self.check("natural_language_remove_removed_exact_monitor",
                   isinstance(removed, dict) and removed.get("thread") == self.thread
                   and not removed.get("monitors"),
                   status=removed)
        self.report["removed_status"] = removed

        before_removed_delivery = resumed_delivery_id
        self.file_path.write_text("change-after-remove\n")
        time.sleep(max(1.0, self.args.remove_observation_seconds))
        after_removed = self.helper("monitor", "list", "--thread", self.thread)
        after_removed_events = set((self.http("/v1/status").get("events") or {}).keys())
        self.check("removed_change_created_no_new_receipt_or_history",
                   not after_removed.get("monitors")
                   and after_removed_events == before_removed_events
                   and self.native_client_count(first_client) == 1
                   and self.native_client_count(resumed_client) == 1)
        self.report["checks"]["removed_change_created_no_new_receipt"] = True
        self.step("removed_monitor_stayed_removed_after_file_change",
                   prior_delivery_id=before_removed_delivery)

        self.report["result"] = "PASS"
        self.step("natural_language_skill_workflow_completed", result="PASS")

    def archive_owned_thread(self):
        if not self.rpc or not self.thread:
            return
        try:
            result = self.rpc.call("thread/archive", {"threadId": self.thread})
            self.report["archive"] = {"requested": True, "response": result, "verified": True}
            self.step("owned_test_thread_archived", thread=self.thread)
        except Exception as exc:
            self.report["archive"] = {"requested": True, "verified": False,
                                       "error": f"{type(exc).__name__}: {exc}"}

    def cleanup(self):
        if self.terminal:
            self.capture("last")
            self.terminal.close()
            self.terminal = None
        self.stop_receiver()
        if self.rpc:
            self.archive_owned_thread()
            self.rpc.close()
            self.rpc = None
        if self.work:
            # The report and terminal capture live outside this disposable tree.
            path = self.work
            import shutil
            shutil.rmtree(path, ignore_errors=True)
            self.report["temporary_workspace"] = str(path)
            self.report["temporary_workspace_removed"] = not path.exists()

    def finish(self):
        self.report["finished_at"] = utc_now()
        self.save()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)

    def run(self):
        self.setup()
        self.discover_thread_and_start_tui()
        self.ensure_initialized()
        self.run_workflow()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--monitor-bin", type=Path, default=DEFAULT_MONITOR_BIN)
    parser.add_argument("--skill-helper", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--reasoning-effort", default=REASONING,
                        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--pause-observation-seconds", type=float, default=3)
    parser.add_argument("--remove-observation-seconds", type=float, default=3)
    args = parser.parse_args()
    for name in ("monitor_bin", "skill_helper", "python"):
        value = getattr(args, name)
        if not value.is_absolute() or not value.exists():
            parser.error(f"--{name.replace('_', '-')} must be an existing absolute path")
    if args.timeout < 10:
        parser.error("--timeout must be at least 10 seconds")
    if args.pause_observation_seconds < 1 or args.remove_observation_seconds < 1:
        parser.error("observation intervals must be at least one second")
    canary = Canary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            canary.step("natural_language_skill_workflow_failed", error=canary.report["error"])
        except Exception:
            pass
    finally:
        canary.cleanup()
        canary.finish()
    return 0 if canary.report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
