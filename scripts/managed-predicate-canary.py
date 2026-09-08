#!/usr/bin/env python3
"""Installed-wheel canary for conversation-scoped JSON predicate monitors.

The canary uses a disposable fake App Server only as a queue/history peer. It
proves predicate selection, stable debounce, recovery, invalid-input handling,
restart/pause age reset, and conversation isolation. It makes no model or UI
claim and records only structured state and content digests in its report.
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
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fake_app_server.py"
THREAD_A = "thread-user"
THREAD_B = "thread-other"
DEBOUNCE = 1.0
POINTER = "/build/status"
EXPECTED = '"failed"'


class CanaryError(RuntimeError):
    pass


def now():
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


class Canary:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-predicate-", dir="/tmp"))
        self.state = self.work / "state"
        self.fake_state = self.work / "fake-app-server.json"
        self.fake_bin = self.work / "fake-bin"
        self.environment = {}
        self.port = None
        self.admin_token = ""
        self.receiver = None
        self.receiver_log = None
        self.report = {
            "result": "RUNNING",
            "scope": "installed wheel / real receiver / JSON predicate monitor",
            "evidence_kind": "fake App Server protocol and process evidence; no model or UI claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
            "checks": {},
            "steps": [],
            "started_at": now(),
            "python": str(args.python),
            "predicate": {
                "json_pointer": POINTER,
                "operator": "eq",
                "value": "failed",
                "debounce_seconds": DEBOUNCE,
            },
        }
        if args.wheel_sha256:
            self.report["wheel_sha256"] = args.wheel_sha256

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    def step(self, name, **details):
        value = {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                 "at": now(), **details}
        self.report["steps"].append(value)
        self.save()
        print(json.dumps({"progress": value}), flush=True)

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
            except (OSError, json.JSONDecodeError, CanaryError) as exc:
                last_error = exc
            time.sleep(.05)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def setup(self):
        self.fake_bin.mkdir(mode=0o700)
        launcher = self.fake_bin / "codex"
        # The launcher is intentionally simple and only receives paths created
        # by this canary. Use a shell-safe command without interpolated input.
        launcher.write_text(
            "#!/bin/sh\nexec \"" + str(self.args.python).replace('"', '\\"') + "\" \""
            + str(FAKE_APP_SERVER).replace('"', '\\"') + "\" \""
            + str(self.fake_state).replace('"', '\\"') + "\"\n"
        )
        launcher.chmod(0o700)
        env = os.environ.copy()
        for key in ("PYTHONPATH", "CODEX_THREAD_ID", "CODEX_MONITOR_HOME", "CODEX_SQLITE_HOME",
                    "CODEX_MONITOR_SERVER_TOKEN", "CODEX_MONITOR_SERVER_TOKEN_FILE"):
            env.pop(key, None)
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        self.environment = env
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
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
        self.report["process_model"] = {"receiver": "separate serve process", "fake_peer": "separate stdio process"}
        self.cli("init", "--port", str(self.port))
        self.admin_token = (self.state / "admin.token").read_text().strip()
        self.cli("source", "managed")
        self.step("installed_runtime_initialized", port=self.port, state=str(self.state))

    def cli(self, *argv, expected=0, timeout=30):
        process = subprocess.run(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv],
            cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=timeout,
        )
        if process.returncode != expected:
            raise CanaryError(f"CLI {argv!r} exited {process.returncode}, expected {expected}: {process.stderr[:2000]}")
        return decode_json(process.stdout) if process.stdout.strip() else None

    def monitor(self, action, name, *, thread, file=None, interval=.1, expected=0):
        argv = ["monitor", action, name, "--thread", thread]
        if file is not None:
            argv.extend(["--file", str(file), "--interval", str(interval), "--debounce", str(DEBOUNCE),
                         "--json-pointer", POINTER, "--operator", "eq", "--value", EXPECTED])
        return self.cli(*argv, expected=expected)

    def status(self, name, thread):
        return self.monitor("status", name, thread=thread)

    def condition(self, name, thread):
        value = self.status(name, thread).get("condition")
        return value if isinstance(value, dict) else {}

    def pending(self, name, thread):
        condition = self.condition(name, thread)
        candidate = condition.get("candidate")
        return condition.get("pending") is True or candidate is not None

    def invalid(self, name, thread):
        status = self.status(name, thread)
        condition = status.get("condition") if isinstance(status.get("condition"), dict) else {}
        return bool(status.get("last_sample_error") or status.get("condition_error") or condition.get("error"))

    def state_json(self):
        if not self.fake_state.exists():
            return {"history": [], "queued": []}
        try:
            value = json.loads(self.fake_state.read_text())
            return value if isinstance(value, dict) else {"history": [], "queued": []}
        except (FileNotFoundError, json.JSONDecodeError):
            return {"history": [], "queued": []}

    def history(self, thread):
        return [row for row in self.state_json().get("history", []) if row.get("threadId") == thread]

    def history_digest_count(self, thread, value):
        return sum(value in json.dumps(row, ensure_ascii=False) for row in self.history(thread))

    def history_state_count(self, thread, state):
        """Count rendered current condition states, excluding ``previous``."""
        count = 0
        needle = f"condition: {state}"
        for row in self.history(thread):
            text = "\n".join(
                part.get("text", "") for part in row.get("content", [])
                if isinstance(part, dict)
            )
            current = text.split("current:", 1)[1].split("path:", 1)[0] if "current:" in text else ""
            if needle in current:
                count += 1
        return count

    def last_delivery_id(self, name, thread):
        value = self.status(name, thread).get("last_delivery") or {}
        return value.get("delivery_id")

    def redacted_public_state(self, name, thread):
        status = self.status(name, thread)
        predicate = status.get("predicate")
        condition = status.get("condition")
        if not isinstance(predicate, dict) or set(predicate) != {"pointer", "operator"}:
            return False
        if condition is not None:
            if not isinstance(condition, dict):
                return False
            candidate = condition.get("candidate")
            if candidate is not None:
                sample = candidate.get("sample") if isinstance(candidate, dict) else None
                if sample not in ({"condition": "matched"}, {"condition": "not_matched"}):
                    return False
        return True

    def redacted_event_state(self, thread):
        """Ensure only predicate state appears in rendered current data."""
        for row in self.history(thread):
            text = "\n".join(
                part.get("text", "") for part in row.get("content", [])
                if isinstance(part, dict)
            )
            current = text.split("current:", 1)[1].split("path:", 1)[0] if "current:" in text else ""
            if "failed" in current.lower() or "selected" in current.lower():
                return False
        return True

    def status_ready(self):
        try:
            request = __import__("urllib.request", fromlist=["Request"]).Request(
                f"http://127.0.0.1:{self.port}/v1/status",
                headers={"Authorization": "Bearer " + self.admin_token},
            )
            with __import__("urllib.request", fromlist=["urlopen"]).urlopen(request, timeout=3) as response:
                value = json.load(response)
            return value if value.get("worker_error") is None else None
        except Exception:
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
            self.receiver.wait(timeout=20)
        self.receiver = None
        if self.receiver_log:
            self.receiver_log.close()
            self.receiver_log = None

    def run(self):
        build_a = self.work / "build-a.json"
        build_b = self.work / "build-b.json"
        baseline = {"build": {"status": "passed"}, "meta": {"seq": 0, "owner": "predicate"}}
        build_a.write_text(json.dumps(baseline) + "\n")
        build_b.write_text(json.dumps(baseline) + "\n")
        self.monitor("create", "build", thread=THREAD_A, file=build_a)
        self.monitor("create", "build", thread=THREAD_B, file=build_b)
        self.start_receiver()
        # Predicate mode deliberately exposes only the selected state in the
        # public sample; the document hash remains an internal sampler detail.
        self.wait_for(lambda: (self.status("build", THREAD_A).get("last_sample") or {}).get("condition") == "not_matched",
                      "conversation A predicate baseline")
        self.wait_for(lambda: (self.status("build", THREAD_B).get("last_sample") or {}).get("condition") == "not_matched",
                      "conversation B predicate baseline")
        self.check("predicate_initial_false_is_silent", not self.history(THREAD_A) and not self.history(THREAD_B))
        self.check("predicate_status_redacts_expected_value", self.redacted_public_state("build", THREAD_A))

        failed_a = {"build": {"status": "failed"}, "meta": {"seq": 1, "owner": "predicate"}}
        replace_text(build_a, json.dumps(failed_a) + "\n")
        self.wait_for(lambda: self.pending("build", THREAD_A), "false-to-true predicate candidate")
        time.sleep(DEBOUNCE * .5)
        self.check("predicate_true_waits_for_debounce", self.history_state_count(THREAD_A, "matched") == 0)
        self.wait_for(lambda: self.history_state_count(THREAD_A, "matched") == 1,
                      "predicate true event after debounce")
        matched_delivery_a = self.last_delivery_id("build", THREAD_A)
        self.check("predicate_false_to_true_emits_once",
                   self.history_state_count(THREAD_A, "matched") == 1 and bool(matched_delivery_a))
        self.check("predicate_event_redacts_selected_value",
                   self.redacted_event_state(THREAD_A))

        for seq in (2, 3, 4):
            replace_text(build_a, json.dumps({"build": {"status": "failed"}, "meta": {"seq": seq, "owner": "predicate"}}) + "\n")
            time.sleep(.25)
        time.sleep(DEBOUNCE * .5)
        self.check("unrelated_json_writes_do_not_reset_true",
                   self.history_state_count(THREAD_A, "matched") == 1
                   and self.last_delivery_id("build", THREAD_A) == matched_delivery_a)

        recovered = {"build": {"status": "passed"}, "meta": {"seq": 5, "owner": "predicate"}}
        replace_text(build_a, json.dumps(recovered) + "\n")
        self.wait_for(lambda: self.history_state_count(THREAD_A, "not_matched") == 1,
                      "predicate recovery event")
        recovered_delivery_a = self.last_delivery_id("build", THREAD_A)
        self.check("predicate_recovery_emits_once",
                   self.history_state_count(THREAD_A, "not_matched") == 1
                   and recovered_delivery_a and recovered_delivery_a != matched_delivery_a)
        self.check("predicate_recovery_event_redacts_expected_value",
                   self.redacted_event_state(THREAD_A))

        # Missing and malformed samples are invalid observations. They must not
        # advance a true candidate's stable age or emit raw invalid content.
        matched_before_invalid = self.history_state_count(THREAD_A, "matched")
        candidate = {"build": {"status": "failed"}, "meta": {"seq": 6, "owner": "predicate"}}
        replace_text(build_a, json.dumps(candidate) + "\n")
        self.wait_for(lambda: self.pending("build", THREAD_A), "invalid-input candidate")
        time.sleep(DEBOUNCE * .5)
        build_a.unlink()
        self.wait_for(lambda: self.invalid("build", THREAD_A), "missing predicate sample")
        time.sleep(DEBOUNCE * 1.2)
        self.check("missing_sample_does_not_credit_condition_age",
                   self.history_state_count(THREAD_A, "matched") == matched_before_invalid)
        replace_text(build_a, "{ malformed\n")
        self.wait_for(lambda: self.invalid("build", THREAD_A), "malformed predicate sample")
        time.sleep(DEBOUNCE * .5)
        self.check("malformed_sample_does_not_emit_raw_event",
                   self.history_state_count(THREAD_A, "matched") == matched_before_invalid
                   and self.redacted_event_state(THREAD_A))
        restored = {"build": {"status": "failed"}, "meta": {"seq": 7, "owner": "predicate"}}
        replace_text(build_a, json.dumps(restored) + "\n")
        self.wait_for(lambda: self.pending("build", THREAD_A), "restored predicate candidate")
        time.sleep(DEBOUNCE * .5)
        self.check("recovered_input_restarts_condition_age", self.history_state_count(THREAD_A, "matched") == matched_before_invalid)
        self.wait_for(lambda: self.history_state_count(THREAD_A, "matched") == matched_before_invalid + 1,
                      "restored predicate event")

        # A pending condition keeps its sample but restart and pause/resume
        # must begin a fresh observed-age window.
        replace_text(build_a, json.dumps(recovered | {"meta": {"seq": 8, "owner": "predicate"}}) + "\n")
        self.wait_for(lambda: self.history_state_count(THREAD_A, "not_matched") == 2, "second predicate recovery")
        pending_restart = {"build": {"status": "failed"}, "meta": {"seq": 9, "owner": "predicate"}}
        replace_text(build_a, json.dumps(pending_restart) + "\n")
        self.wait_for(lambda: self.pending("build", THREAD_A), "restart-age candidate")
        time.sleep(DEBOUNCE * .5)
        self.stop_receiver(kill=True)
        self.start_receiver()
        self.wait_for(lambda: self.pending("build", THREAD_A), "restart candidate restored")
        time.sleep(DEBOUNCE * .6)
        restart_matched_before = self.history_state_count(THREAD_A, "matched")
        self.check("restart_resets_condition_age", self.history_state_count(THREAD_A, "matched") == restart_matched_before)
        self.wait_for(lambda: self.history_state_count(THREAD_A, "matched") == restart_matched_before + 1,
                      "restart candidate event")

        paused = {"build": {"status": "passed"}, "meta": {"seq": 10, "owner": "predicate"}}
        replace_text(build_a, json.dumps(paused) + "\n")
        self.wait_for(lambda: self.history_state_count(THREAD_A, "not_matched") == 3, "pause reset recovery")
        pending_pause = {"build": {"status": "failed"}, "meta": {"seq": 11, "owner": "predicate"}}
        replace_text(build_a, json.dumps(pending_pause) + "\n")
        self.wait_for(lambda: self.pending("build", THREAD_A), "pause-age candidate")
        time.sleep(DEBOUNCE * .5)
        self.monitor("pause", "build", thread=THREAD_A)
        time.sleep(DEBOUNCE * 1.2)
        self.monitor("resume", "build", thread=THREAD_A)
        self.wait_for(lambda: self.pending("build", THREAD_A), "pause candidate restored")
        time.sleep(DEBOUNCE * .6)
        pause_matched_before = self.history_state_count(THREAD_A, "matched")
        self.check("pause_resume_resets_condition_age", self.history_state_count(THREAD_A, "matched") == pause_matched_before)
        self.wait_for(lambda: self.history_state_count(THREAD_A, "matched") == pause_matched_before + 1,
                      "pause candidate event")

        failed_b = {"build": {"status": "failed"}, "meta": {"seq": 1, "owner": "predicate-b"}}
        replace_text(build_b, json.dumps(failed_b) + "\n")
        self.wait_for(lambda: self.history_state_count(THREAD_B, "matched") == 1, "conversation B predicate event")
        self.check("predicate_events_are_conversation_scoped",
                   self.history_state_count(THREAD_A, "matched") >= 1
                   and self.history_state_count(THREAD_B, "matched") == 1
                   and self.last_delivery_id("build", THREAD_B) != self.last_delivery_id("build", THREAD_A))
        status_a = self.status("build", THREAD_A)
        status_b = self.status("build", THREAD_B)
        self.check("predicate_status_is_thread_scoped",
                   status_a.get("thread") == THREAD_A and status_b.get("thread") == THREAD_B)
        self.check("predicate_status_and_events_exclude_raw_expected_content",
                   self.redacted_public_state("build", THREAD_A)
                   and self.redacted_public_state("build", THREAD_B)
                   and self.redacted_event_state(THREAD_A)
                   and self.redacted_event_state(THREAD_B))
        self.report["request_files"] = {"conversation_a": str(build_a), "conversation_b": str(build_b)}
        self.report["core_result"] = "PASS"
        self.report.setdefault("phase_results", {})["core"] = "PASS"
        self.step("managed_predicate_canary_completed", result="PASS")

    def run_soak(self):
        requested = self.args.soak_seconds
        if requested <= 0:
            return
        path = self.work / "build-a.json"
        baseline_events = self.history_state_count(THREAD_A, "matched")
        baseline_delivery = self.last_delivery_id("build", THREAD_A)
        started = time.monotonic()
        deadline = started + requested
        interval = max(1.0, min(10.0, requested / 30))
        next_write = started
        writes = 0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_write:
                writes += 1
                replace_text(path, json.dumps({
                    "build": {"status": "failed"},
                    "meta": {"seq": 1000 + writes, "owner": "predicate-soak"},
                }) + "\n")
                next_write += interval
                if self.history_state_count(THREAD_A, "matched") != baseline_events:
                    raise CanaryError("predicate soak emitted a duplicate matched event")
                if self.last_delivery_id("build", THREAD_A) != baseline_delivery:
                    raise CanaryError("predicate soak changed the stable delivery identity")
                if not self.status_ready():
                    raise CanaryError("receiver became unhealthy during predicate soak")
                self.step("predicate_soak_sample", writes=writes,
                           remaining_seconds=round(max(0, deadline - time.monotonic()), 1))
                continue
            if not self.status_ready():
                raise CanaryError("receiver became unhealthy during predicate soak")
            time.sleep(min(.25, max(0, deadline - time.monotonic())))
        self.report["soak"] = {
            "requested_seconds": requested,
            "actual_seconds": round(time.monotonic() - started, 3),
            "writes": writes,
            "matched_events_before": baseline_events,
            "matched_events_after": self.history_state_count(THREAD_A, "matched"),
            "delivery_id": baseline_delivery,
        }
        self.check("predicate_soak_keeps_true_state_stable", self.report["soak"]["matched_events_after"] == baseline_events)

    def run_tui(self):
        """Consume one redacted predicate event in an ordinary owned TUI."""
        tui_report = {
            "result": "FAIL",
            "surface": "ordinary Codex TUI in owned disposable PTY",
            "evidence_kind": "native history and pyte terminal evidence; no model completion claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
        }
        terminal = receiver = rpc = remote_server = receiver_log = None
        try:
            import importlib.util
            helper_spec = importlib.util.spec_from_file_location(
                "predicate_tui_helpers", ROOT / "scripts" / "tui-canary.py"
            )
            if helper_spec is None or helper_spec.loader is None:
                raise CanaryError("cannot load owned PTY helper")
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
            Terminal = helper.Terminal
            RemoteServer = helper.RemoteServer
            from codex_monitor.session import Rpc

            work = Path(tempfile.mkdtemp(prefix="codex-monitor-predicate-tui-", dir="/tmp")).resolve()
            state = work / "state"
            file_path = work / "build.json"
            file_path.write_text(json.dumps({"build": {"status": "passed"}, "meta": {"seq": 0}}) + "\n")
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
                    raise CanaryError(f"predicate TUI CLI {argv!r} failed: {process.stderr.strip()[:1200]}")
                return decode_json(process.stdout) if process.stdout.strip() else None

            tui_cli("init", "--port", str(port))
            endpoint = "shared-local"
            tui_argv = [
                "codex", "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never",
                "-c", f"model_reasoning_effort=\"{self.args.reasoning_effort}\"",
                "-c", "tui.animations=false",
            ]
            instruction = (
                "This is an isolated owned-PTY predicate acceptance test. Do not use tools or read files. "
                "Reply exactly READY_PREDICATE_TUI to this initial prompt. For the later user marker "
                "PREDICATE_TUI_FOLLOWUP, reply exactly ACK_PREDICATE_TUI_FOLLOWUP."
            )
            if self.args.remote:
                remote_server = RemoteServer(work / "app-server.sock")
                endpoint = remote_server.endpoint
                tui_argv[1:1] = ["--remote", endpoint]
            if self.args.model:
                tui_argv[1:1] = ["--model", self.args.model]
            terminal = Terminal(tui_argv + [instruction])
            rpc = Rpc(endpoint)
            thread = None
            trust_sent = False
            trust_prompt_seen = False
            command_approval_seen = False

            def turns():
                return rpc.call("thread/turns/list", {
                    "threadId": thread, "itemsView": "full", "limit": 100,
                })["data"] if thread else []

            def native_text(kind=None):
                values = []
                for turn in turns():
                    for item in turn.get("items", []):
                        if kind is not None and item.get("type") != kind:
                            continue
                        values.append(item.get("text", "") if item.get("type") == "agentMessage"
                                     else json.dumps(item.get("content", []), ensure_ascii=False))
                return "\n".join(values)

            def pump_until(condition, label, timeout=None):
                nonlocal trust_sent, trust_prompt_seen, command_approval_seen
                deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
                while time.monotonic() < deadline:
                    terminal.pump(.1)
                    screen = terminal.text()
                    lower = screen.lower()
                    approval_prompt = (
                        "would you like to run the following command" in lower
                        or "allow command" in lower
                        or ("approve this command" in lower and "deny" in lower)
                    )
                    if approval_prompt:
                        command_approval_seen = True
                        raise CanaryError(
                            "predicate TUI requested command approval; no approval was sent"
                        )
                    trust_modal = (
                        "trust this folder" in lower
                        or "trust the contents" in lower
                        or "trust this workspace" in lower
                        or "do you trust" in lower
                    )
                    trusted_workspace = str(work).lower() in lower
                    if trusted_workspace and trust_modal and (
                        "yes, i trust" in lower or "yes, continue" in lower
                    ):
                        trust_prompt_seen = True
                    if not trust_sent and trusted_workspace and trust_modal and (
                        "yes, i trust" in lower or "yes, continue" in lower
                    ):
                        terminal.input("\r")
                        trust_sent = True
                    if condition():
                        return
                raise CanaryError(f"{label} timed out")

            def find_thread():
                nonlocal thread
                params = {"cwd": str(work), "limit": 10}
                if not self.args.remote:
                    params["sourceKinds"] = ["cli"]
                rows = rpc.call("thread/list", params)["data"]
                if len(rows) == 1:
                    thread = rows[0]["id"]
                return thread

            pump_until(find_thread, "predicate TUI thread discovery")
            pump_until(lambda: "READY_PREDICATE_TUI" in native_text("agentMessage"),
                       "predicate TUI initial response")
            source = tui_cli("source", "predicate")
            source_token = Path(source["token_file"]).read_text().strip()
            created = tui_cli(
                "monitor", "create", "predicate", "--thread", thread, "--file", str(file_path),
                "--interval", "0.2", "--debounce", str(DEBOUNCE), "--endpoint", endpoint,
                "--json-pointer", POINTER, "--operator", "eq", "--value", EXPECTED,
            )
            if created.get("endpoint") != endpoint:
                raise CanaryError("predicate TUI monitor did not persist endpoint")
            admin = (state / "admin.token").read_text().strip()
            receiver_log = (work / "serve.log").open("w")
            receiver = subprocess.Popen(
                [str(self.args.python), "-m", "codex_monitor", "--state", str(state), "serve"],
                cwd=work, env=env, stdout=receiver_log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def receiver_ready():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/status",
                    headers={"Authorization": "Bearer " + admin},
                )
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return json.load(response).get("worker_error") is None
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    return False

            def delivery_state(delivery_id):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/deliveries/{delivery_id}",
                    headers={"Authorization": "Bearer " + admin},
                )
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return json.load(response)
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    return None

            pump_until(receiver_ready, "predicate TUI receiver readiness", timeout=30)
            pump_until(lambda: (tui_cli("monitor", "status", "predicate", "--thread", thread)
                                .get("last_sample") or {}).get("condition") == "not_matched",
                       "predicate TUI silent baseline", timeout=30)

            def current_block(text):
                return text.rsplit("current:", 1)[1].split("path:", 1)[0] if "current:" in text else ""

            def rendered_state(state):
                current = current_block(terminal.text())
                return f"condition: {state}" in current and "failed" not in current.lower()

            def native_state_count(state, client_id=None):
                needle = f"condition: {state}"
                count = 0
                for turn in turns():
                    for item in turn.get("items", []):
                        if item.get("type") != "userMessage":
                            continue
                        text = json.dumps(item.get("content", []), ensure_ascii=False)
                        current = current_block(text)
                        if needle in current:
                            if "failed" in current.lower():
                                return -1
                            if client_id is None or item.get("clientId") == client_id:
                                count += 1
                return count

            def predicate_status():
                return tui_cli("monitor", "status", "predicate", "--thread", thread)

            def predicate_status_state():
                return (predicate_status().get("last_sample") or {}).get("condition")

            matched_file = {"build": {"status": "failed"}, "meta": {"seq": 1}}
            file_path.write_text(json.dumps(matched_file) + "\n")
            pump_until(lambda: predicate_status_state() == "matched"
                       and (predicate_status().get("last_delivery") or {}).get("delivery_id"),
                       "predicate TUI matched state", timeout=30)
            matched_delivery = predicate_status().get("last_delivery") or {}
            matched_delivery_id = matched_delivery.get("delivery_id")
            matched_client_id = "codex-monitor:" + matched_delivery_id
            pump_until(lambda: (delivery_state(matched_delivery_id) or {}).get("state") == "accepted",
                       "matched predicate delivery accepted", timeout=30)
            pump_until(lambda: rendered_state("matched"),
                       "redacted matched predicate event rendered in TUI", timeout=60)
            matched_rendered = terminal.text()
            pump_until(lambda: native_state_count("matched", matched_client_id) == 1,
                       "matched predicate event consumed in native history", timeout=60)

            recovered_file = {"build": {"status": "passed"}, "meta": {"seq": 2}}
            file_path.write_text(json.dumps(recovered_file) + "\n")
            pump_until(lambda: predicate_status_state() == "not_matched"
                       and (predicate_status().get("last_delivery") or {}).get("delivery_id")
                       and (predicate_status().get("last_delivery") or {}).get("delivery_id") != matched_delivery_id,
                       "predicate TUI recovered state", timeout=30)
            recovered_delivery = predicate_status().get("last_delivery") or {}
            recovered_delivery_id = recovered_delivery.get("delivery_id")
            recovered_client_id = "codex-monitor:" + recovered_delivery_id
            pump_until(lambda: (delivery_state(recovered_delivery_id) or {}).get("state") == "accepted",
                       "recovered predicate delivery accepted", timeout=30)
            pump_until(lambda: rendered_state("not_matched"),
                       "redacted recovered predicate event rendered in TUI", timeout=60)
            recovered_rendered = terminal.text()
            pump_until(lambda: native_state_count("not_matched", recovered_client_id) == 1,
                       "recovered predicate event consumed in native history", timeout=60)
            self.check(
                "predicate_tui_renders_matched_and_recovered_once",
                "condition: matched" in matched_rendered
                and "condition: not_matched" in recovered_rendered
                and native_state_count("matched", matched_client_id) == 1
                and native_state_count("not_matched", recovered_client_id) == 1
                and matched_delivery_id != recovered_delivery_id,
            )
            self.check(
                "predicate_tui_event_states_redact_selected_value",
                "failed" not in current_block(matched_rendered).lower()
                and "failed" not in current_block(recovered_rendered).lower(),
            )
            terminal.input("PREDICATE_TUI_FOLLOWUP")
            for _ in range(5):
                terminal.pump(.1)
            terminal.input("\r")
            pump_until(lambda: "ACK_PREDICATE_TUI_FOLLOWUP" in native_text("agentMessage"),
                       "predicate TUI follow-up response", timeout=60)
            tui_report.update(
                result="PASS", client_ui_verified=True, model_delivery_verified=False,
                endpoint=endpoint, thread=thread, event_consumed=True,
                matched_event_consumed=True, recovered_event_consumed=True,
                matched_delivery_id=matched_delivery_id,
                recovered_delivery_id=recovered_delivery_id,
                matched_client_id=matched_client_id,
                recovered_client_id=recovered_client_id,
                matched_native_count=native_state_count("matched", matched_client_id),
                recovered_native_count=native_state_count("not_matched", recovered_client_id),
                model_saw_matched_recovered_once=True,
                trust_prompt_seen=trust_prompt_seen,
                command_approval_seen=command_approval_seen,
                approval_policy="never",
                followup_response_seen=True, process_model={
                    "tui_pid": terminal.pid, "receiver_pid": receiver.pid,
                    **({"remote_app_server_pid": remote_server.pid} if remote_server else {}),
                },
            )
            self.report["checks"]["predicate_event_consumed_in_ordinary_tui"] = True
            self.report["checks"]["predicate_tui_followup_after_recovery"] = True
            self.step("predicate_tui_event_consumed", thread=thread, endpoint=endpoint)
        except ModuleNotFoundError as exc:
            if exc.name == "pyte":
                tui_report.update(result="SKIPPED", reason="optional PTY dependency pyte is not installed")
                self.step("predicate_tui_skipped", reason=tui_report["reason"])
            else:
                raise
        finally:
            if terminal is not None:
                buffer_path = self.args.report.with_suffix(".tui-terminal.txt")
                try:
                    buffer_path.write_text(terminal.text())
                    tui_report["terminal_buffer_capture"] = str(buffer_path)
                except OSError:
                    pass
                terminal.close()
            if receiver is not None and receiver.poll() is None:
                try:
                    os.killpg(receiver.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    receiver.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(receiver.pid, signal.SIGKILL)
                    receiver.wait(timeout=5)
            if receiver_log is not None:
                receiver_log.close()
            if rpc is not None:
                rpc.close()
            if remote_server is not None:
                remote_server.close()
            self.report["tui"] = tui_report

    def cleanup(self):
        if self.receiver is not None:
            self.stop_receiver(kill=True)
        if self.work.exists():
            self.report["temporary_work"] = str(self.work)
            import shutil
            shutil.rmtree(self.work, ignore_errors=True)
            self.report["temporary_work_removed"] = True

    def finish(self):
        self.report["finished_at"] = now()
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.save()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--soak-seconds", type=int, default=0,
                        help="optional real elapsed predicate stability soak")
    parser.add_argument("--tui", action="store_true",
                        help="also run an ordinary owned TUI predicate event check")
    parser.add_argument("--remote", action="store_true",
                        help="run the TUI check through an owned Unix App Server")
    parser.add_argument("--model", help="optional model override for the owned TUI")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        default="low",
        help="reasoning effort for the owned TUI (default: low)",
    )
    args = parser.parse_args()
    if not args.python.is_absolute() or not args.python.is_file() or not os.access(args.python, os.X_OK):
        parser.error("--python must be an absolute executable installed-runtime Python")
    if args.soak_seconds < 0:
        parser.error("--soak-seconds must be non-negative")
    if args.remote and not args.tui:
        parser.error("--remote requires --tui")
    canary = Canary(args)
    final_result = "FAIL"
    try:
        canary.setup()
        canary.run()
        canary.run_soak()
        if args.soak_seconds > 0:
            canary.report.setdefault("phase_results", {})["soak"] = "PASS"
            canary.save()
        if args.tui:
            canary.run_tui()
            if canary.report.get("tui", {}).get("result") != "PASS":
                final_result = "INCOMPLETE"
                canary.report.setdefault("phase_results", {})["tui"] = canary.report.get("tui", {}).get("result", "FAIL")
            else:
                canary.report.setdefault("phase_results", {})["tui"] = "PASS"
            canary.save()
        if final_result != "INCOMPLETE":
            final_result = "PASS"
    except Exception as exc:
        final_result = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            canary.report["diagnostic_status"] = {
                thread: canary.status("build", thread)
                for thread in (THREAD_A, THREAD_B)
            }
            canary.report["diagnostic_history"] = {
                thread: canary.history(thread)
                for thread in (THREAD_A, THREAD_B)
            }
        except Exception as diagnostic_exc:
            canary.report["diagnostic_status_error"] = f"{type(diagnostic_exc).__name__}: {diagnostic_exc}"
        canary.report["steps"].append({"name": "managed_predicate_canary_failed",
                                       "elapsed_seconds": round(time.monotonic() - canary.started, 3),
                                       "at": now(), "error": canary.report["error"]})
    finally:
        try:
            canary.cleanup()
        except Exception as cleanup_exc:
            final_result = "FAIL"
            canary.report["cleanup_error"] = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        canary.report["result"] = final_result
        canary.finish()
    return 0 if canary.report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
