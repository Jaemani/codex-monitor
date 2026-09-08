#!/usr/bin/env python3
"""Bounded process-sampler isolation canary for an installed wheel.

The canary supplies a test-only ``sitecustomize`` module to the receiver's
child processes.  It makes one regular-file read block forever and another
read complete after a controlled delay.  The production receiver must keep a
healthy monitor moving, cancel the blocked work on pause, and discard delayed
results after pause or removal.  A fourth monitor covers durable debounce,
reversion, restart re-anchoring, and an outage that must not count toward its
stable window.  The fixture never replaces production
modules and the report is process/protocol evidence only: the fake App Server
peer does not invoke a model or provide UI evidence.

Run this against the Python executable from an isolated installation, for
example ``python3 scripts/managed-monitor-isolation-canary.py --python
/tmp/monitor-venv/bin/python --report /tmp/isolation.json``.  The canary
intentionally fails against an implementation that samples synchronously in
the receiver process.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fake_app_server.py"


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


def replace_text(path: Path, value: str):
    temporary = path.with_name(path.name + ".next")
    temporary.write_text(value)
    os.replace(temporary, path)


def decode_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CanaryError(f"CLI did not return JSON: {raw[:1000]!r}") from exc


def quote_for_sh(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


FIXTURE = r'''
"""Test-only read controls for the process-sampler isolation canary.

This module is installed in a disposable PYTHONPATH supplied only to the
canary's receiver and its descendants.  It targets exact paths and leaves all
other Python I/O untouched.
"""
import builtins
import json
import os
from pathlib import Path
import stat
import time

_open = os.open
_fstat = os.fstat
_read = os.read
_close = os.close
_tracked = {}
_pipe_writers = {}
_blocked_fds = set()
_marker = os.environ.get("CM_CANARY_MARKER")
_hang_path = os.path.realpath(os.environ.get("CM_CANARY_HANG_PATH", ""))
_delay_path = os.path.realpath(os.environ.get("CM_CANARY_DELAY_PATH", ""))
_outage_path = os.path.realpath(os.environ.get("CM_CANARY_OUTAGE_PATH", ""))
_outage_control = os.environ.get("CM_CANARY_OUTAGE_CONTROL", "")
_delay_seconds = float(os.environ.get("CM_CANARY_DELAY_SECONDS", "2.0"))


def _emit(event, path, fd):
    if not _marker:
        return
    row = {"event": event, "path": path, "fd": fd, "pid": os.getpid(), "at": time.time()}
    try:
        with builtins.open(_marker, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
    except OSError:
        pass


def open_wrapper(path, flags, mode=0o777, *, dir_fd=None):
    try:
        resolved = os.path.realpath(os.fspath(path))
    except (TypeError, ValueError):
        resolved = ""
    outage_active = resolved == _outage_path and _outage_control and os.path.exists(_outage_control)
    if resolved == _hang_path or outage_active:
        read_fd, write_fd = os.pipe()
        _pipe_writers[read_fd] = write_fd
        _tracked[read_fd] = resolved
        _blocked_fds.add(read_fd)
        _emit("outage_opened" if outage_active else "opened", resolved, read_fd)
        return read_fd
    if dir_fd is None:
        fd = _open(path, flags, mode)
    else:
        fd = _open(path, flags, mode, dir_fd=dir_fd)
    if resolved == _delay_path:
        _tracked[fd] = resolved
        _emit("opened", resolved, fd)
    return fd


def fstat_wrapper(fd):
    if fd in _pipe_writers:
        # Make the controlled pipe look like a one-byte regular file to the
        # production sampler. The subsequent os.read is a real blocking OS
        # read, and the process sampler must terminate the child that owns it.
        return os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, 1, 0, 0, 0))
    return _fstat(fd)


def read_wrapper(fd, size):
    path = _tracked.get(fd)
    if fd in _blocked_fds:
        _emit("outage_entered" if path == _outage_path else "hang_entered", path, fd)
        return _read(fd, size)
    if path == _delay_path:
        _emit("delay_entered", path, fd)
        time.sleep(_delay_seconds)
    return _read(fd, size)


def close_wrapper(fd):
    _tracked.pop(fd, None)
    _blocked_fds.discard(fd)
    writer = _pipe_writers.pop(fd, None)
    try:
        return _close(fd)
    finally:
        if writer is not None:
            try:
                _close(writer)
            except OSError:
                pass


os.open = open_wrapper
os.fstat = fstat_wrapper
os.read = read_wrapper
os.close = close_wrapper
'''


class Canary:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-isolation-", dir="/tmp"))
        self.state = self.work / "state"
        self.fake_state = self.work / "fake-app-server.json"
        self.marker = self.work / "sampler-markers.jsonl"
        self.fake_bin = self.work / "fake-bin"
        self.fixture = self.work / "fixture"
        self.receiver: subprocess.Popen | None = None
        self.receiver_log = None
        self.environment: dict[str, str] = {}
        self.port: int | None = None
        self.admin_token = ""
        self.hang_path = self.work / "hung.txt"
        self.delay_path = self.work / "late.txt"
        self.healthy_path = self.work / "healthy.txt"
        self.policy_path = self.work / "policy.txt"
        self.outage_control = self.work / "enable-outage"
        self.report = {
            "result": "RUNNING",
            "scope": "installed wheel / real receiver / process-sampler isolation",
            "evidence_kind": "process and fake App Server protocol evidence; no model or UI claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
            "checks": {},
            "steps": [],
            "started_at": now(),
            "python": str(args.python),
            "fixture": {
                "mode": "test-only sitecustomize exact-path read controls",
                "hung_path": str(self.hang_path),
                "delayed_path": str(self.delay_path),
                "outage_path": str(self.policy_path),
                "outage_control": str(self.outage_control),
                "delay_seconds": args.delay_seconds,
                "debounce_seconds": args.debounce_seconds,
            },
        }
        if args.wheel_sha256:
            self.report["wheel_sha256"] = args.wheel_sha256

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(self.report, indent=2) + "\n")

    def step(self, name, **details):
        item = {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "at": now(), **details}
        self.report["steps"].append(item)
        self.save()
        print(json.dumps({"progress": item}), flush=True)

    def check(self, name, condition, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def wait_for(self, condition, label, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else self.args.timeout)
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (OSError, urllib.error.URLError, CanaryError) as exc:
                last_error = exc
            time.sleep(.05)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def setup_environment(self):
        self.fixture.mkdir()
        (self.fixture / "sitecustomize.py").write_text(FIXTURE)
        self.fake_bin.mkdir()
        fake_codex = self.fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\nexec " + quote_for_sh(sys.executable) + " "
            + quote_for_sh(str(FAKE_APP_SERVER)) + " " + quote_for_sh(str(self.fake_state)) + "\n"
        )
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("CODEX_THREAD_ID", None)
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(self.fixture)
        env["CM_CANARY_MARKER"] = str(self.marker)
        env["CM_CANARY_HANG_PATH"] = str(self.hang_path)
        env["CM_CANARY_DELAY_PATH"] = str(self.delay_path)
        env["CM_CANARY_OUTAGE_PATH"] = str(self.policy_path)
        env["CM_CANARY_OUTAGE_CONTROL"] = str(self.outage_control)
        env["CM_CANARY_DELAY_SECONDS"] = str(self.args.delay_seconds)
        self.environment = env

        probe = subprocess.run(
            [str(self.args.python), "-c", "import codex_monitor; print(codex_monitor.__file__)"],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=20,
        )
        if probe.returncode:
            raise CanaryError(f"installed-runtime probe failed: {probe.stderr.strip()[:1000]}")
        package = Path(probe.stdout.strip()).resolve()
        if package.is_relative_to(ROOT):
            raise CanaryError(f"runtime imports checkout source, not an installed wheel: {package}")
        self.report["installed_package"] = str(package)
        self.report["fake_app_server"] = str(FAKE_APP_SERVER)
        self.report["process_model"] = {
            "receiver": "separate serve process",
            "fake_peer": "separate stdio process",
            "sampler_fixture": "child-visible exact-path read delay/hang",
        }

    def cli(self, *argv, expected=0, timeout=20):
        command = [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv]
        process = subprocess.run(command, cwd=self.work, env=self.environment,
                                 capture_output=True, text=True, timeout=timeout)
        if process.returncode != expected:
            raise CanaryError(
                f"CLI {argv!r} exited {process.returncode}, expected {expected}: "
                f"{process.stderr.strip()[:2000]}"
            )
        return decode_json(process.stdout) if process.stdout.strip() else None

    def monitor(self, action, name, thread="thread-user", expected=0, file=None, interval=.1, debounce=None):
        argv = ["monitor", action, name, "--thread", thread]
        if file is not None:
            argv.extend(["--file", str(file), "--interval", str(interval)])
        if debounce is not None:
            argv.extend(["--debounce", str(debounce)])
        return self.cli(*argv, expected=expected)

    def status(self, name, thread="thread-user"):
        return self.monitor("status", name, thread=thread)

    def condition_pending(self):
        value = self.status("policy", "thread-policy").get("condition")
        return bool(value and value.get("pending"))

    def state_json(self):
        if not self.fake_state.exists():
            return {"history": [], "queued": []}
        try:
            return json.loads(self.fake_state.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"history": [], "queued": []}

    def history(self, thread):
        return [row for row in self.state_json().get("history", []) if row.get("threadId") == thread]

    def history_has(self, thread, value):
        return any(value in json.dumps(row, ensure_ascii=False) for row in self.history(thread))

    def history_count(self, thread, value):
        return sum(value in json.dumps(row, ensure_ascii=False) for row in self.history(thread))

    def markers(self):
        if not self.marker.exists():
            return []
        rows = []
        for line in self.marker.read_text().splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def marker_seen(self, event, path, after=0):
        target = os.path.realpath(os.fspath(path))
        rows = [row for row in self.markers() if row.get("event") == event and row.get("path") == target]
        return rows[after:]

    @staticmethod
    def pids_gone(pids, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    remaining.append(pid)
                else:
                    remaining.append(pid)
            if not remaining:
                return True
            time.sleep(.05)
        return False

    def status_ready(self):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/status",
                headers={"Authorization": "Bearer " + self.admin_token},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                value = json.load(response)
            return value if value.get("worker_error") is None else None
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def start_receiver(self):
        self.receiver_log = (self.work / "serve.log").open("a")
        self.receiver = subprocess.Popen(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), "serve"],
            cwd=self.work, env=self.environment, stdout=self.receiver_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.wait_for(self.status_ready, "receiver readiness")
        self.step("receiver_started", pid=self.receiver.pid)

    def stop_receiver(self, kill=False):
        if self.receiver is None:
            return
        if self.receiver.poll() is None:
            try:
                os.killpg(self.receiver.pid, signal.SIGKILL if kill else signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.receiver.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.receiver.pid, signal.SIGKILL)
                self.receiver.wait(timeout=15)
        self.receiver = None
        if self.receiver_log:
            self.receiver_log.close()
            self.receiver_log = None

    def run(self):
        try:
            self.setup_environment()
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
            self.hang_path.write_text("hung-baseline\n")
            self.delay_path.write_text("late-baseline\n")
            self.healthy_path.write_text("healthy-baseline\n")
            self.policy_path.write_text("policy-baseline\n")
            self.cli("init", "--port", str(self.port))
            self.admin_token = (self.state / "admin.token").read_text().strip()
            self.cli("source", "managed")
            self.monitor("create", "hung", thread="thread-user", file=self.hang_path)
            self.monitor("create", "late", thread="thread-late", file=self.delay_path)
            self.monitor("create", "healthy", thread="thread-healthy", file=self.healthy_path)
            self.monitor("create", "policy", thread="thread-policy", file=self.policy_path,
                         debounce=self.args.debounce_seconds)
            self.start_receiver()

            self.wait_for(lambda: self.marker_seen("hang_entered", self.hang_path),
                          "hung sampler child start")
            first_hung_pids = sorted({row["pid"] for row in self.marker_seen("hang_entered", self.hang_path)})
            self.step("hung_sampler_child_entered", pids=first_hung_pids)
            healthy_baseline = digest(self.healthy_path)
            self.wait_for(lambda: (self.status("healthy", "thread-healthy").get("last_sample") or {}).get("sha256") == healthy_baseline,
                          "healthy baseline beside hung sampler")
            self.check("healthy_monitor_baseline_progresses_beside_hung_sampler", True)

            self.healthy_path.write_text("healthy-change-one\n")
            healthy_one = digest(self.healthy_path)
            self.wait_for(lambda: self.history_has("thread-healthy", healthy_one),
                          "healthy change beside hung sampler")
            self.check("healthy_monitor_change_progresses_beside_hung_sampler", True)

            # A receiver crash must terminate its in-flight sampler children,
            # including children that elected to start a new session.
            self.stop_receiver(kill=True)
            self.check("receiver_sigkill_cleans_hung_sampler_children",
                       self.pids_gone(first_hung_pids, self.args.late_wait + 2))
            self.start_receiver()
            self.wait_for(lambda: (self.status("healthy", "thread-healthy").get("last_sample") or {}).get("sha256") == healthy_one,
                          "healthy checkpoint after receiver restart")
            self.check("receiver_restart_does_not_duplicate_healthy_event",
                       self.history_count("thread-healthy", healthy_one) == 1)
            self.wait_for(lambda: len(self.marker_seen("hang_entered", self.hang_path)) > len(first_hung_pids),
                          "hung sampler child after receiver restart")

            self.monitor("pause", "hung", thread="thread-user")
            self.wait_for(lambda: not self.status("hung", "thread-user").get("enabled", True),
                          "hung monitor pause")
            self.wait_for(lambda: self.status("hung", "thread-user").get("collector_status") == "stopped",
                          "hung monitor worker stop after pause")
            self.hang_path.write_text("hung-after-pause\n")
            hung_after_pause = digest(self.hang_path)
            time.sleep(self.args.late_wait)
            self.check("pause_discards_hung_sampler_late_result",
                       not self.history_has("thread-user", hung_after_pause))

            late_baseline = digest(self.delay_path)
            self.wait_for(lambda: (self.status("late", "thread-late").get("last_sample") or {}).get("sha256") == late_baseline,
                          "delayed monitor baseline")
            self.delay_path.write_text("late-after-pause\n")
            late_one = digest(self.delay_path)
            marker_before = len(self.marker_seen("delay_entered", self.delay_path))
            self.wait_for(lambda: len(self.marker_seen("delay_entered", self.delay_path)) > marker_before,
                          "delayed sampler child start")
            self.monitor("pause", "late", thread="thread-late")
            time.sleep(self.args.delay_seconds + self.args.late_wait)
            self.check("pause_discards_delayed_sampler_late_result",
                       not self.history_has("thread-late", late_one))

            self.monitor("resume", "late", thread="thread-late")
            replace_text(self.delay_path, "late-before-rapid-pause\n")
            late_two = digest(self.delay_path)
            marker_before = len(self.marker_seen("delay_entered", self.delay_path))
            self.wait_for(lambda: len(self.marker_seen("delay_entered", self.delay_path)) > marker_before,
                          "rapid pause/resume delayed sampler child start")
            self.monitor("pause", "late", thread="thread-late")
            self.monitor("resume", "late", thread="thread-late")
            replace_text(self.delay_path, "late-after-rapid-resume\n")
            late_after_resume = digest(self.delay_path)
            self.wait_for(lambda: self.history_has("thread-late", late_after_resume),
                          "current delayed result after rapid pause/resume")
            time.sleep(self.args.delay_seconds + self.args.late_wait)
            self.check("rapid_pause_resume_discards_stale_delayed_result",
                       not self.history_has("thread-late", late_two))

            replace_text(self.delay_path, "late-before-remove\n")
            late_three = digest(self.delay_path)
            marker_before = len(self.marker_seen("delay_entered", self.delay_path))
            self.wait_for(lambda: len(self.marker_seen("delay_entered", self.delay_path)) > marker_before,
                          "remove delayed sampler child start")
            self.monitor("remove", "late", thread="thread-late")
            time.sleep(self.args.delay_seconds + self.args.late_wait)
            self.check("remove_discards_delayed_sampler_late_result",
                       not self.history_has("thread-late", late_three))

            policy_baseline = digest(self.policy_path)
            self.wait_for(lambda: (self.status("policy", "thread-policy").get("last_sample") or {}).get("sha256") == policy_baseline,
                          "debounce monitor baseline")
            replace_text(self.policy_path, "policy-candidate-revert\n")
            policy_revert = digest(self.policy_path)
            self.wait_for(self.condition_pending, "debounce candidate pending")
            time.sleep(self.args.debounce_seconds * .5)
            self.check("debounce_candidate_does_not_emit_early",
                       not self.history_has("thread-policy", policy_revert))
            replace_text(self.policy_path, "policy-baseline\n")
            self.wait_for(lambda: not self.condition_pending(), "debounce candidate reversion")
            self.check("debounce_reversion_cancels_candidate",
                       not self.history_has("thread-policy", policy_revert))

            replace_text(self.policy_path, "policy-stable-change\n")
            policy_stable = digest(self.policy_path)
            self.wait_for(self.condition_pending, "stable debounce candidate")
            self.wait_for(lambda: self.history_has("thread-policy", policy_stable),
                          "stable debounce receipt")
            self.check("stable_change_emits_after_debounce",
                       self.history_count("thread-policy", policy_stable) == 1)

            replace_text(self.policy_path, "policy-restart-pending\n")
            policy_restart = digest(self.policy_path)
            self.wait_for(self.condition_pending, "restart debounce candidate")
            self.stop_receiver(kill=True)
            self.start_receiver()
            self.wait_for(self.condition_pending, "restart candidate restored")
            time.sleep(self.args.debounce_seconds * .5)
            self.check("restart_does_not_credit_pending_downtime",
                       not self.history_has("thread-policy", policy_restart))
            self.wait_for(lambda: self.history_has("thread-policy", policy_restart),
                          "restart candidate stable receipt")
            self.check("restart_candidate_emits_once_after_new_observations",
                       self.history_count("thread-policy", policy_restart) == 1)

            replace_text(self.policy_path, "policy-outage-pending\n")
            policy_outage = digest(self.policy_path)
            self.wait_for(self.condition_pending, "outage debounce candidate")
            self.outage_control.write_text("enabled\n")
            self.wait_for(lambda: self.marker_seen("outage_entered", self.policy_path),
                          "unhealthy sampler outage")
            time.sleep(self.args.debounce_seconds + self.args.late_wait)
            self.check("unhealthy_sampler_does_not_credit_debounce",
                       not self.history_has("thread-policy", policy_outage))
            self.outage_control.unlink(missing_ok=True)
            self.wait_for(lambda: self.history_has("thread-policy", policy_outage),
                          "outage candidate stable receipt")
            self.check("recovered_sampler_emits_outage_candidate_once",
                       self.history_count("thread-policy", policy_outage) == 1)

            self.healthy_path.write_text("healthy-change-two\n")
            healthy_two = digest(self.healthy_path)
            self.wait_for(lambda: self.history_has("thread-healthy", healthy_two),
                          "healthy change after pause and removal")
            self.check("healthy_monitor_remains_live_after_pause_and_remove", True)
            self.check("receiver_remains_healthy", self.status_ready() is not None)

            self.report["markers"] = self.markers()
            self.report["processes"] = {"receiver_pid": self.receiver.pid if self.receiver else None}
            self.report["result"] = "PASS"
            self.step("process_sampler_isolation_completed", result="PASS")
        finally:
            self.stop_receiver(kill=True)
            pids = sorted({row.get("pid") for row in self.markers() if isinstance(row.get("pid"), int)})
            remaining = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                alive = []
                for pid in pids:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    except PermissionError:
                        alive.append(pid)
                    else:
                        alive.append(pid)
                remaining = alive
                if not remaining:
                    break
                time.sleep(.05)
            self.report.setdefault("cleanup", {})["sampler_pids"] = pids
            self.report["cleanup"]["remaining_sampler_pids"] = remaining
            if self.report.get("result") == "RUNNING":
                self.report["result"] = "FAIL"
            self.report["finished_at"] = now()
            self.save()
            print(json.dumps({"result": self.report["result"], "report": str(self.args.report),
                              "remaining_sampler_pids": remaining}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", required=True,
        help="explicitly opt in to the process-isolation canary",
    )
    parser.add_argument("--python", required=True, type=Path,
                        help="Python executable from the isolated installed wheel")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--late-wait", type=float, default=.5,
                        help="settle time after pause/remove, in seconds")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--debounce-seconds", type=float, default=1.0)
    parser.add_argument("--wheel-sha256")
    args = parser.parse_args()
    if args.timeout <= 0 or args.late_wait < 0 or args.delay_seconds <= 0 or args.debounce_seconds <= 0:
        parser.error("timeouts and delay must be positive")
    if not args.python.exists():
        parser.error(f"Python executable does not exist: {args.python}")
    canary = Canary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        canary.report["steps"].append({"name": "process_sampler_isolation_failed",
                                       "elapsed_seconds": round(time.monotonic() - canary.started, 3),
                                       "at": now(), "error": canary.report["error"]})
        canary.report["result"] = "FAIL"
        canary.save()
        print(json.dumps({"result": "FAIL", "report": str(args.report),
                          "error": canary.report["error"]}), flush=True)
        return 1
    return 0 if canary.report.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
