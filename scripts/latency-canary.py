#!/usr/bin/env python3
"""Bounded latency observations for a managed file monitor and real Codex TUI.

The canary owns a disposable monitor state directory, a receiver process, an
owned Unix App Server, and one ordinary ``codex --remote`` TUI in a PTY.  It
records observation timestamps at five separate surfaces for each file
change:

* producer/file change;
* local collector checkpoint intake;
* HTTP/local delivery acceptance;
* exact native history consumption by client id; and
* visible rendering in the PTY buffer.

Samples are deliberately bounded and grouped as ``idle``, ``busy`` or
``restart``.  The result is an observed distribution only.  It does not make
an API latency guarantee and does not measure model completion quality.

The command is opt-in because it starts a real model-backed TUI.  It uses
read-only/never approval settings, never approves command prompts, and only
accepts the workspace-trust prompt for its own disposable directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
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
WATCH_NAME = "latency-watch"
DEFAULT_SAMPLE_COUNT = 6
MIN_SAMPLE_COUNT = 5
MAX_SAMPLE_COUNT = 10
SAMPLE_CATEGORIES = ("idle", "busy", "restart")


class CanaryError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CanaryError(f"CLI did not return JSON: {raw[:1000]!r}") from exc


def import_tui_helpers():
    path = ROOT / "scripts" / "tui-canary.py"
    spec = importlib.util.spec_from_file_location("latency_tui_helpers", path)
    if spec is None or spec.loader is None:
        raise CanaryError(f"cannot load PTY helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Terminal, module.RemoteServer


def percentile(values: list[float], quantile: float) -> float:
    """Linear observed percentile; the caller supplies a nonempty list."""

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report = {
            "result": "RUNNING",
            "scope": "installed receiver / owned Unix App Server / ordinary Codex TUI",
            "surface": "direct codex --remote TUI in an owned disposable PTY",
            "evidence_kind": (
                "observed monotonic intervals correlated with local checkpoint, HTTP delivery, "
                "native history and PTY rendering"
            ),
            "latency_claim": "observed samples only; no latency guarantee",
            "sample_plan": {
                "requested_count": args.samples,
                "categories": list(SAMPLE_CATEGORIES),
            },
            "checks": {},
            "steps": [],
            "samples": [],
            "started_at": utc_now(),
            "process_model": {"harness_pid": os.getpid()},
            "python": str(args.python),
        }
        self.work: Path | None = None
        self.state: Path | None = None
        self.file_path: Path | None = None
        self.env: dict[str, str] | None = None
        self.port: int | None = None
        self.admin_token: str | None = None
        self.endpoint: str | None = None
        self.terminal = None
        self.remote_server = None
        self.rpc = None
        self.thread: str | None = None
        self.receiver: subprocess.Popen | None = None
        self.receiver_logs: list[object] = []
        self.Terminal = None
        self.RemoteServer = None
        self.trust_sent = False
        self.terminal_buffers: dict[str, str] = {}

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.report["updated_at"] = utc_now()
        temporary = self.args.report.with_suffix(self.args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(self.args.report)

    def step(self, name: str, **details):
        value = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "at": utc_now(),
            **details,
        }
        self.report["steps"].append(value)
        self.save()
        print(json.dumps({"progress": value}, ensure_ascii=False), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    @staticmethod
    def point(monotonic_value: float) -> dict[str, object]:
        return {"monotonic_seconds": round(monotonic_value, 9), "utc": utc_now()}

    def cli(self, *argv: str, expected: int = 0, timeout: float = 30):
        if self.state is None or self.work is None or self.env is None:
            raise CanaryError("canary runtime is not initialized")
        process = subprocess.run(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if process.returncode != expected:
            raise CanaryError(
                f"CLI {argv!r} exited {process.returncode}, expected {expected}: "
                f"{process.stderr.strip()[:2000]}"
            )
        return json_value(process.stdout) if process.stdout.strip() else None

    def http(self, path: str):
        if self.port is None or self.admin_token is None:
            raise CanaryError("HTTP endpoint is not initialized")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": "Bearer " + self.admin_token},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def status_ready(self):
        try:
            value = self.http("/v1/status")
            return value if value.get("worker_error") is None else None
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def wait_for(self, condition, label: str, timeout: float | None = None,
                 *, pump: bool = True):
        deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
        last_error = None
        while time.monotonic() < deadline:
            try:
                if pump and self.terminal is not None:
                    self.terminal.pump(.05)
                    self.check_tui_safety()
                value = condition()
                if value:
                    return value
            except CanaryError:
                raise
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
            time.sleep(.02)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def clean_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("CODEX_THREAD_ID", None)
        return env

    def setup(self):
        self.Terminal, self.RemoteServer = import_tui_helpers()
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-latency-", dir="/tmp")).resolve()
        self.state = self.work / "state"
        self.file_path = self.work / "watched.txt"
        self.file_path.write_text("latency-baseline\n")
        self.env = self.clean_environment()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]

        probe = subprocess.run(
            [str(self.args.python), "-c", "import codex_monitor; print(codex_monitor.__file__)"],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe.returncode:
            raise CanaryError(f"installed-runtime probe failed: {probe.stderr.strip()[:1000]}")
        package = Path(probe.stdout.strip()).resolve()
        if package.is_relative_to(ROOT):
            raise CanaryError(f"runtime imports checkout source, not installed wheel: {package}")
        self.report["installed_package"] = str(package)

        self.cli("init", "--port", str(self.port))
        self.admin_token = (self.state / "admin.token").read_text().strip()
        self.endpoint = None
        self.remote_server = self.RemoteServer(self.work / "app-server.sock")
        self.endpoint = self.remote_server.endpoint
        self.report["process_model"].update({
            "receiver": "separate installed serve process",
            "owner_app_server": "separate codex app-server process on Unix socket",
            "remote_endpoint": self.endpoint,
            "remote_app_server_pid": self.remote_server.pid,
        })
        instruction = (
            "This is an isolated latency observation in a disposable workspace. "
            "Never use tools, run commands, read files, or contact services. "
            "Reply exactly READY_LATENCY_CANARY to this initial prompt. "
            "For a later user message beginning BUSY_SAMPLE, write at least 1000 words of "
            "careful, detailed response before ending with BUSY_DONE plus its suffix. "
            "When a managed monitor event arrives, do not call tools."
        )
        argv = [
            "codex", "--remote", self.endpoint, "-C", str(self.work), "--no-alt-screen",
            "-s", "read-only", "-a", "never",
            "-c", f'model_reasoning_effort="{self.args.reasoning_effort}"',
            "-c", "tui.animations=false", instruction,
        ]
        if self.args.model:
            argv[1:1] = ["--model", self.args.model]
        self.terminal = self.Terminal(argv)
        self.report["process_model"]["tui_pid"] = self.terminal.pid
        self.rpc = self._rpc(self.endpoint)
        self.wait_for(self.find_thread, "owned remote TUI thread discovery", timeout=90)
        self.wait_for(
            lambda: self.native_agent_has("READY_LATENCY_CANARY"),
            "initial native model response",
            timeout=90,
        )
        self.check("owned_remote_thread_discovered", bool(self.thread), thread=self.thread)
        loaded = self.rpc.call("thread/loaded/list", {})["data"]
        self.check("owner_has_exact_loaded_thread", self.thread in loaded, loaded_thread_ids=loaded)

        self.cli(
            "monitor", "create", WATCH_NAME, "--thread", self.thread,
            "--file", str(self.file_path), "--interval", str(self.args.interval),
            "--endpoint", self.endpoint,
        )
        self.start_receiver()
        baseline = digest(self.file_path)
        self.wait_for(
            lambda: (self.status().get("last_sample") or {}).get("sha256") == baseline,
            "silent baseline checkpoint",
            timeout=30,
        )
        self.check("baseline_checkpoint_is_silent", not self.status().get("last_delivery"))
        self.step("latency_canary_ready", thread=self.thread, endpoint=self.endpoint)

    def _rpc(self, endpoint):
        from codex_monitor.session import Rpc
        return Rpc(endpoint)

    def status(self):
        return self.cli("monitor", "status", WATCH_NAME, "--thread", self.thread or "")

    def find_thread(self):
        rows = self.rpc.call("thread/list", {"cwd": str(self.work), "limit": 10})["data"]
        if len(rows) == 1:
            self.thread = rows[0]["id"]
        return self.thread

    def turns(self):
        return self.rpc.call(
            "thread/turns/list", {"threadId": self.thread, "itemsView": "full", "limit": 100}
        )["data"]

    def native_agent_has(self, text: str) -> bool:
        return any(
            item.get("type") == "agentMessage" and item.get("text", "").strip() == text
            for turn in self.turns() for item in turn.get("items", [])
        )

    def native_client_occurrences(self, client_id: str) -> list[dict]:
        return [
            item
            for turn in self.turns()
            for item in turn.get("items", [])
            if item.get("type") == "userMessage" and item.get("clientId") == client_id
        ]

    def check_tui_safety(self):
        """Reject command approvals; accept only this run's trust modal."""

        if self.terminal is None:
            return
        screen = self.terminal.text()
        lower = screen.lower()
        # Never press a key on a command approval or permission dialog.  The
        # TUI is started with -a never and the prompt forbids tools, so seeing
        # one is an unsafe/unexpected run condition.
        approval_prompt = (
            "would you like to run the following command" in lower
            or "allow command" in lower
            or ("approve this command" in lower and "deny" in lower)
        )
        if approval_prompt:
            raise CanaryError("unexpected command approval prompt; no approval was sent")
        # Only the one trust dialog for this exact disposable workspace is
        # eligible for Enter.  A path check prevents an unrelated trust modal
        # from being accepted, and trust_sent makes the action one-shot even
        # if the rendered modal remains in the terminal buffer.
        trusted_workspace = self.work is not None and str(self.work).lower() in lower
        trust_modal = (
            "trust this folder" in lower
            or "trust the contents" in lower
            or "trust this workspace" in lower
            or "do you trust" in lower
        )
        if (
            not self.trust_sent
            and trusted_workspace
            and trust_modal
            and ("yes, i trust" in lower or "yes, continue" in lower)
        ):
            self.terminal.input("\r")
            self.trust_sent = True

    def screen_idle(self) -> bool:
        if self.terminal is None:
            return False
        screen = self.terminal.text().lower()
        return (
            "ask codex to do anything" in screen
            and not any(turn.get("status") == "inProgress" for turn in self.turns())
        )

    def screen_busy(self, marker: str | None = None) -> bool:
        if self.terminal is None:
            return False
        screen = self.terminal.text()
        lower = screen.lower()
        if marker is not None:
            exact_marker = "› " + marker
            assistant_rendered = any(
                line.lstrip().startswith(("• ", "● ")) for line in screen.splitlines()
            )
            return (
                exact_marker in screen
                and assistant_rendered
                and "ask codex to do anything" not in lower
            )
        return "ask codex to do anything" not in lower and "esc to interrupt" in lower

    def start_receiver(self):
        if self.work is None or self.state is None or self.env is None:
            raise CanaryError("receiver runtime is not initialized")
        log_path = self.work / f"serve-{len(self.receiver_logs)}.log"
        log = log_path.open("w")
        self.receiver_logs.append(log)
        self.receiver = subprocess.Popen(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), "serve"],
            cwd=self.work,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.wait_for(self.status_ready, "receiver readiness", timeout=30, pump=False)
        if self.receiver.poll() is not None:
            raise CanaryError(f"receiver exited with {self.receiver.returncode}; see {log_path}")
        self.step("receiver_started", pid=self.receiver.pid, log=str(log_path))

    def stop_receiver(self, *, kill: bool = False):
        process = self.receiver
        if process is None or process.poll() is not None:
            self.receiver = None
            return
        try:
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        self.receiver = None

    def restart_receiver(self):
        old_pid = self.receiver.pid if self.receiver is not None else None
        self.stop_receiver()
        self.start_receiver()
        self.step("receiver_restarted", old_pid=old_pid, new_pid=self.receiver.pid)

    def write_file(self, content: str) -> tuple[str, float]:
        if self.file_path is None:
            raise CanaryError("watched file is not initialized")
        with self.file_path.open("w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        changed_at = time.monotonic()
        return digest(self.file_path), changed_at

    def submit_busy_turn(self, marker: str) -> float:
        if self.terminal is None:
            raise CanaryError("TUI is not initialized")
        self.wait_for(lambda: self.screen_idle(), "TUI idle before busy sample", timeout=60)
        text = f"BUSY_SAMPLE {marker}"
        self.terminal.input(text)
        for _ in range(4):
            self.terminal.pump(.05)
            self.check_tui_safety()
        self.terminal.input("\r")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.terminal.pump(.03)
            self.check_tui_safety()
            # The composer remains visible during streaming, and the input
            # can scroll out of view. Require the exact submitted input in an
            # active native turn rather than inferring activity from a footer.
            active = [turn for turn in self.turns() if turn.get("status") == "inProgress"]
            if any(
                item.get("type") == "userMessage"
                and text in json.dumps(item.get("content", []), ensure_ascii=False)
                for turn in active for item in turn.get("items", [])
            ):
                return time.monotonic()
            time.sleep(.02)
        raise CanaryError(f"busy user turn {marker} was not confirmed active")

    def observe_sample(self, category: str, index: int) -> dict:
        if category not in SAMPLE_CATEGORIES:
            raise CanaryError(f"unknown sample category: {category}")
        if self.thread is None:
            raise CanaryError("native thread is not initialized")

        self.wait_for(lambda: self.screen_idle(), "TUI idle before latency sample", timeout=60)
        restart_started = None
        restart_completed = None
        if category == "restart":
            restart_started = time.monotonic()
            self.restart_receiver()
            restart_completed = time.monotonic()
            self.wait_for(self.status_ready, "receiver readiness after restart", timeout=30, pump=False)
        busy_marker = f"{index:02d}"
        busy_active_at = None
        if category == "busy":
            busy_active_at = self.submit_busy_turn(busy_marker)

        marker_text = f"latency-{category}-{index}-{time.time_ns()}"
        marker, changed_at = self.write_file(marker_text + "\n")
        sample = {
            "index": index,
            "category": category,
            "marker": marker,
            "producer_file_change": self.point(changed_at),
            "scenario_note": (
                "receiver restarted before producer/file change; post-restart warm path"
                if category == "restart" else None
            ),
            "receiver_restart_started": self.point(restart_started) if restart_started else None,
            "receiver_restart_completed": self.point(restart_completed) if restart_completed else None,
            "busy_turn_marker": busy_marker if category == "busy" else None,
            "busy_native_active_observed": self.point(busy_active_at) if busy_active_at else None,
            "delivery_id": None,
            "client_id": None,
            "submission_id": None,
        }
        local_at = accepted_at = consumed_at = visible_at = None
        delivery = None
        deadline = time.monotonic() + self.args.timeout
        next_status = 0.0
        latest_status = None
        while time.monotonic() < deadline:
            self.terminal.pump(.03)
            self.check_tui_safety()
            if time.monotonic() >= next_status:
                latest_status = self.status()
                status_observed_at = time.monotonic()
                next_status = status_observed_at + .08
            else:
                status_observed_at = None
            if local_at is None and latest_status:
                last_sample = latest_status.get("last_sample") or {}
                if last_sample.get("sha256") == marker:
                    local_at = status_observed_at or time.monotonic()
                    sample["local_collector_checkpoint"] = self.point(local_at)
            if delivery is None and latest_status:
                candidate = latest_status.get("last_delivery") or {}
                candidate_id = candidate.get("delivery_id")
                if candidate_id:
                    candidate_event = self.http("/v1/deliveries/" + candidate_id)
                    delivery_observed_at = time.monotonic()
                    current = ((candidate_event.get("envelope") or {}).get("data") or {}).get("current") or {}
                    if current.get("sha256") == marker:
                        delivery = candidate_event
                        sample["delivery_id"] = candidate_id
                        sample["client_id"] = candidate_event.get("client_id")
                        sample["delivery_event_state_when_seen"] = candidate_event.get("state")
                        sample["delivery_id_observed"] = self.point(delivery_observed_at)
            if delivery is not None and accepted_at is None:
                current_delivery = self.http("/v1/deliveries/" + delivery["id"])
                accepted_observed_at = time.monotonic()
                if current_delivery.get("state") == "accepted":
                    accepted_at = accepted_observed_at
                    delivery = current_delivery
                    sample["submission_id"] = current_delivery.get("submission_id")
                    sample["http_native_acceptance"] = self.point(accepted_at)
            client_id = sample.get("client_id")
            if client_id and consumed_at is None:
                occurrences = self.native_client_occurrences(client_id)
                consumed_observed_at = time.monotonic()
                if occurrences:
                    if len(occurrences) != 1:
                        raise CanaryError(
                            f"native history contains {len(occurrences)} copies of {client_id}"
                        )
                    consumed_at = consumed_observed_at
                    sample["native_history_consumption"] = self.point(consumed_at)
            screen = self.terminal.text()
            visible_observed_at = time.monotonic()
            if visible_at is None and marker in screen:
                visible_at = visible_observed_at
                sample["visible_tui_rendering"] = self.point(visible_at)
            if local_at and accepted_at and consumed_at and visible_at:
                break
        if not all((local_at, accepted_at, consumed_at, visible_at, delivery)):
            raise CanaryError(
                f"latency sample incomplete category={category} index={index}: "
                f"local={local_at!r} accepted={accepted_at!r} "
                f"consumed={consumed_at!r} visible={visible_at!r} "
                f"delivery={sample.get('delivery_id')!r}"
            )
        if sample.get("client_id") is None or sample.get("submission_id") is None:
            raise CanaryError("accepted delivery did not expose stable client and submission IDs")
        final_status = self.status()
        if category == "busy":
            submitted = f"BUSY_SAMPLE {busy_marker}"
            matching_inputs = [
                item for turn in self.turns() for item in turn.get("items", [])
                if item.get("type") == "userMessage"
                and submitted in json.dumps(item.get("content", []), ensure_ascii=False)
            ]
            if len(matching_inputs) != 1:
                raise CanaryError("busy sample lost its exact submitted user input")
            sample["busy_user_input_correlated_in_native_history"] = True
        if final_status.get("thread") != self.thread or final_status.get("endpoint") != self.endpoint:
            raise CanaryError("managed status lost exact thread or Unix endpoint binding")
        if delivery.get("state") != "accepted":
            raise CanaryError("delivery state changed after acceptance observation")
        inspection = self.cli("inspect", sample["delivery_id"])
        if (
            inspection.get("local", {}).get("client_id") != sample["client_id"]
            or inspection.get("native", {}).get("state") != "consumed"
            or inspection.get("target", {}).get("thread") != self.thread
            or inspection.get("target", {}).get("endpoint") != self.endpoint
        ):
            raise CanaryError(f"installed inspect did not confirm exact consumed target: {inspection!r}")
        sample["native_inspection"] = inspection

        sample["durations_seconds"] = {
            "change_to_local_collector_checkpoint": round(local_at - changed_at, 6),
            "local_collector_checkpoint_to_http_native_acceptance": round(accepted_at - local_at, 6),
            "http_native_acceptance_to_native_history_consumption": round(consumed_at - accepted_at, 6),
            "native_history_consumption_to_visible_tui_rendering": round(visible_at - consumed_at, 6),
            "change_to_visible_tui_rendering": round(visible_at - changed_at, 6),
        }
        sample["timing_note"] = (
            "Intervals are signed differences between independent observer timestamps. "
            "A negative gap is an observer-order artifact, not a product latency claim."
        )
        self.report["samples"].append(sample)
        self.save()
        self.step(
            "latency_sample_completed",
            index=index,
            category=category,
            delivery_id=sample["delivery_id"],
            client_id=sample["client_id"],
            durations_seconds=sample["durations_seconds"],
        )
        self.wait_for(lambda: self.screen_idle(), "TUI idle after latency sample", timeout=90)
        return sample

    def summarize(self):
        metrics = (
            "change_to_local_collector_checkpoint",
            "local_collector_checkpoint_to_http_native_acceptance",
            "http_native_acceptance_to_native_history_consumption",
            "native_history_consumption_to_visible_tui_rendering",
            "change_to_visible_tui_rendering",
        )
        by_category = {}
        for category in SAMPLE_CATEGORIES:
            rows = [row for row in self.report["samples"] if row["category"] == category]
            observed = {}
            for metric in metrics:
                values = [row["durations_seconds"][metric] for row in rows]
                if not values:
                    continue
                quantiles = {
                    "p50_observed_seconds": round(percentile(values, .50), 6),
                    "min_observed_seconds": round(min(values), 6),
                    "max_observed_seconds": round(max(values), 6),
                }
                if len(values) >= 2:
                    quantiles["p90_observed_seconds"] = round(percentile(values, .90), 6)
                    quantiles["p95_observed_seconds"] = round(percentile(values, .95), 6)
                observed[metric] = quantiles
            by_category[category] = {
                "sample_count": len(rows),
                "observed": observed,
                "small_sample_caveat": (
                    "Quantiles are omitted where fewer than two observations exist; even p95 "
                    "with this bounded sample count is descriptive only."
                ),
            }
        self.report["observed_quantiles"] = by_category
        self.report["quantile_note"] = (
            "Quantiles describe only these bounded observations. They are not latency "
            "SLOs, guarantees, or universal behavior across clients, hosts, or load. "
            "The p95 value is especially unstable for the small per-category sample counts."
        )

    def run(self):
        self.save()
        self.setup()
        for index in range(self.args.samples):
            category = SAMPLE_CATEGORIES[index % len(SAMPLE_CATEGORIES)]
            self.observe_sample(category, index + 1)
        counts = {category: sum(row["category"] == category for row in self.report["samples"])
                  for category in SAMPLE_CATEGORIES}
        self.check("bounded_sample_plan_completed", len(self.report["samples"]) == self.args.samples,
                   counts=counts)
        self.summarize()
        self.report["samples_complete"] = True
        self.step("latency_samples_completed", counts=counts)

    def capture_terminal(self):
        if self.terminal is None:
            return
        self.terminal.pump(0)
        self.terminal_buffers["last"] = self.terminal.text()
        path = self.args.report.with_suffix(".terminal.txt")
        path.write_text("\n\n".join(
            f"=== {name} ===\n{value}" for name, value in self.terminal_buffers.items()
        ))
        self.report["terminal_buffer_capture"] = str(path)

    def archive_owned_thread(self):
        if self.rpc is None or self.thread is None:
            return
        try:
            response = self.rpc.call("thread/archive", {"threadId": self.thread})
            self.report["archive"] = {
                "requested": True,
                "thread": self.thread,
                "response": response,
                "verified": True,
            }
            self.step("owned_test_thread_archived", thread=self.thread)
        except Exception as exc:
            self.report["archive"] = {
                "requested": True,
                "thread": self.thread,
                "verified": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            raise

    def cleanup(self):
        errors = []

        def attempt(label, action):
            try:
                action()
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

        attempt("capture_terminal", self.capture_terminal)
        if self.terminal is not None:
            terminal = self.terminal
            try:
                terminal.close()
            except Exception as exc:
                errors.append(f"terminal.close: {type(exc).__name__}: {exc}")
            finally:
                self.terminal = None
        attempt("stop_receiver", lambda: self.stop_receiver(kill=False))
        for stream in self.receiver_logs:
            attempt("receiver_log.close", stream.close)
        self.receiver_logs.clear()
        if self.rpc is not None:
            attempt("archive_owned_thread", self.archive_owned_thread)
            rpc = self.rpc
            try:
                rpc.close()
            except Exception as exc:
                errors.append(f"rpc.close: {type(exc).__name__}: {exc}")
            finally:
                self.rpc = None
        if self.remote_server is not None:
            remote_server = self.remote_server
            try:
                remote_server.close()
            except Exception as exc:
                errors.append(f"remote_server.close: {type(exc).__name__}: {exc}")
            finally:
                self.remote_server = None
        if self.work is not None:
            self.report["temporary_work"] = str(self.work)
            # Keep the disposable state path in the report while removing the
            # files themselves after every run; failure evidence is the report
            # and terminal capture next to --report.
            import shutil
            try:
                shutil.rmtree(self.work)
            except Exception as exc:
                errors.append(f"temporary_work.remove: {type(exc).__name__}: {exc}")
            removed = not self.work.exists()
            self.report["temporary_work_removed"] = removed
            if not removed and not any(item.startswith("temporary_work.remove:") for item in errors):
                errors.append("temporary_work.remove: path remains after cleanup")
        if errors:
            self.report["cleanup_error"] = "; ".join(errors)
            raise RuntimeError(self.report["cleanup_error"])

    def finish(self):
        self.report["finished_at"] = utc_now()
        self.save()
        if self.report.get("result") == "FAIL":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            preserved = self.args.report.with_name(
                self.args.report.stem + ".failure-" + stamp + self.args.report.suffix
            )
            preserved.write_text(self.args.report.read_text())
            self.report["preserved_failure_report"] = str(preserved)
            self.save()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--python", required=True, type=Path,
                        help="absolute Python from the clean installed wheel")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLE_COUNT,
                        help=f"bounded sample count ({MIN_SAMPLE_COUNT}-{MAX_SAMPLE_COUNT}, default {DEFAULT_SAMPLE_COUNT})")
    parser.add_argument("--interval", type=float, default=.1,
                        help="managed collector interval in seconds (default .1)")
    parser.add_argument("--timeout", type=float, default=60,
                        help="per-sample timeout in seconds (default 60)")
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
    if not MIN_SAMPLE_COUNT <= args.samples <= MAX_SAMPLE_COUNT:
        parser.error(f"--samples must be between {MIN_SAMPLE_COUNT} and {MAX_SAMPLE_COUNT}")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be positive and finite")
    if not math.isfinite(args.timeout) or args.timeout < 10:
        parser.error("--timeout must be finite and at least 10 seconds")

    canary = Canary(args)
    try:
        canary.run()
    except ModuleNotFoundError as exc:
        canary.report["result"] = "SKIPPED" if exc.name == "pyte" else "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
    except FileNotFoundError as exc:
        canary.report["result"] = "SKIPPED" if exc.filename == "codex" else "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            canary.cleanup()
        except Exception as exc:
            canary.report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            canary.report["result"] = "FAIL"
        if canary.report.get("result") == "RUNNING":
            canary.report["result"] = "PASS" if canary.report.get("samples_complete") else "FAIL"
        canary.finish()
    return 0 if canary.report["result"] in {"PASS", "SKIPPED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
