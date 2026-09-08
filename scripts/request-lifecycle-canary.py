#!/usr/bin/env python3
"""Exercise the installed request lifecycle through CLI, HTTP and a real receiver.

The canary uses a disposable fake App Server for protocol evidence and an
installed wheel for the receiver and CLI.  It verifies source and conversation
scope, stable request identities, duplicate and compare-and-set update
handling, terminal ordering, restart recovery, cancellation and expiry.  The
fake peer never invokes a model and does not provide UI evidence.

``--tui`` is a separate opt-in check.  It requires ``pyte`` in the Python
running this script, starts an ordinary Codex TUI in an owned PTY and requires
both native history evidence for the managed request event and a later native
assistant response.  A rendered prompt echo alone is never accepted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
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
THREAD_A = "thread-user"
THREAD_B = "thread-other"
SOURCE_A = "request-a"
SOURCE_B = "request-b"


class CanaryError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def request_id(value):
    """Find a request identity across the small response-shape variants."""

    if isinstance(value, dict):
        for key in ("request_id", "id", "uuid"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for key in ("request", "item", "result"):
            candidate = value.get(key)
            found = request_id(candidate)
            if found:
                return found
    return None


def state_of(value):
    if isinstance(value, dict):
        for key in ("state", "status"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        for key in ("request", "item", "result"):
            candidate = value.get(key)
            found = state_of(candidate)
            if found:
                return found
    return None


def revision_of(value):
    if isinstance(value, dict):
        for key in ("revision", "version"):
            candidate = value.get(key)
            if type(candidate) is int:
                return candidate
        for key in ("request", "item", "result"):
            candidate = value.get(key)
            found = revision_of(candidate)
            if found is not None:
                return found
    return None


def rows_of(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "requests", "items", "results"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.work: Path | None = None
        self.state: Path | None = None
        self.fake_state: Path | None = None
        self.fake_bin: Path | None = None
        self.receiver: subprocess.Popen | None = None
        self.receiver_log = None
        self.environment: dict[str, str] = {}
        self.port: int | None = None
        self.admin_token = ""
        self.source_tokens: dict[str, str] = {}
        self.report = {
            "result": "FAIL",
            "scope": "installed wheel / real receiver / request lifecycle CLI and HTTP",
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

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    def step(self, name: str, **details):
        item = {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "at": now(), **details}
        self.report["steps"].append(item)
        self.save()
        print(json.dumps({"progress": item}), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def wait_for(self, condition, label: str, timeout: float | None = None):
        deadline = time.monotonic() + (timeout if timeout is not None else self.args.timeout)
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (OSError, urllib.error.URLError, json.JSONDecodeError, CanaryError) as exc:
                last_error = exc
            time.sleep(.1)
        suffix = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{suffix}")

    def cli_raw(self, *argv: str, timeout: float = 30):
        if self.state is None or self.work is None:
            raise CanaryError("canary is not initialized")
        process = subprocess.run(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv],
            cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=timeout,
        )
        value = None
        if process.stdout.strip():
            try:
                value = json.loads(process.stdout)
            except json.JSONDecodeError as exc:
                raise CanaryError(f"CLI did not return JSON: {process.stdout[:1000]!r}") from exc
        return process.returncode, value, process.stderr.strip()

    def cli(self, *argv: str, expected: int = 0, timeout: float = 30):
        code, value, error = self.cli_raw(*argv, timeout=timeout)
        if code != expected:
            raise CanaryError(f"CLI {argv!r} exited {code}, expected {expected}: {error[:2000]}")
        return value

    def setup(self):
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-request-", dir="/tmp"))
        self.state = self.work / "state"
        self.fake_state = self.work / "fake-app-server.json"
        self.fake_bin = self.work / "fake-bin"
        self.fake_bin.mkdir(mode=0o700)
        (self.work / "codex-home").mkdir(mode=0o700)

        launcher = self.fake_bin / "codex"
        launcher.write_text(
            "#!/bin/sh\nexec " + shlex.quote(str(self.args.python)) + " "
            + shlex.quote(str(FAKE_APP_SERVER)) + " " + shlex.quote(str(self.fake_state)) + "\n"
        )
        launcher.chmod(0o700)

        env = os.environ.copy()
        for key in (
            "PYTHONPATH", "CODEX_THREAD_ID", "CODEX_MONITOR_HOME", "CODEX_SQLITE_HOME",
            "CODEX_MONITOR_SERVER_TOKEN", "CODEX_MONITOR_SERVER_TOKEN_FILE",
        ):
            env.pop(key, None)
        env["CODEX_HOME"] = str(self.work / "codex-home")
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        self.environment = env

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        probe = subprocess.run(
            [str(self.args.python), "-c", "import codex_monitor; print(codex_monitor.__file__)"],
            cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=20,
        )
        if probe.returncode:
            raise CanaryError(f"installed-runtime probe failed: {probe.stderr.strip()[:1000]}")
        package = Path(probe.stdout.strip()).resolve()
        if package.is_relative_to(ROOT):
            raise CanaryError(f"runtime imports checkout source, not an installed wheel: {package}")
        self.report["installed_package"] = str(package)
        self.report["fake_app_server"] = str(FAKE_APP_SERVER)
        self.report["process_model"] = {"receiver": "separate serve process", "fake_peer": "separate stdio process"}
        self.report["private_paths"] = {"work": str(self.work), "state": str(self.state), "fake_peer_state": str(self.fake_state)}

        self.cli("init", "--port", str(self.port))
        self.admin_token = (self.state / "admin.token").read_text().strip()
        for source in (SOURCE_A, SOURCE_B):
            result = self.cli("source", source)
            token_file = Path(result["token_file"])
            self.source_tokens[source] = token_file.read_text().strip()
        self.cli("bind", "request-a-binding", "--thread", THREAD_A, "--source", SOURCE_A, "--endpoint", "shared-local")
        self.cli("bind", "request-b-binding", "--thread", THREAD_B, "--source", SOURCE_B, "--endpoint", "shared-local")
        self.start_receiver()
        self.step("installed_runtime_initialized", port=self.port, state=str(self.state))

    def start_receiver(self):
        if self.work is None or self.state is None:
            raise CanaryError("receiver state is not initialized")
        if self.receiver is not None and self.receiver.poll() is None:
            return
        self.receiver_log = (self.work / f"serve-{int(time.time() * 1000)}.log").open("a")
        self.receiver = subprocess.Popen(
            [str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), "serve"],
            cwd=self.work, env=self.environment, stdout=self.receiver_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.wait_for(self.receiver_ready, "receiver readiness")
        self.report.setdefault("receiver_pids", []).append(self.receiver.pid)
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

    def http(self, method: str, path: str, payload=None, token: str | None = None):
        if self.port is None:
            raise CanaryError("HTTP port is not initialized")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Authorization": "Bearer " + (token or self.admin_token)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = {"error": raw.decode(errors="replace")}
            return exc.code, value

    def receiver_ready(self):
        try:
            code, value = self.http("GET", "/v1/status")
            return code == 200 and isinstance(value, dict) and value.get("worker_error") is None
        except (OSError, urllib.error.URLError, json.JSONDecodeError, CanaryError):
            return False

    def fake_state_value(self):
        if self.fake_state is None or not self.fake_state.exists():
            return {"history": [], "queued": []}
        for _ in range(30):
            try:
                value = json.loads(self.fake_state.read_text())
                return value if isinstance(value, dict) else {"history": [], "queued": []}
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.03)
        raise CanaryError("fake App Server state is unavailable or malformed")

    def history(self, thread: str):
        return [row for row in self.fake_state_value().get("history", []) if row.get("threadId") == thread]

    def history_has(self, thread: str, marker: str):
        return any(marker in json_text(row) for row in self.history(thread))

    def history_count(self, thread: str, marker: str):
        return sum(marker in json_text(row) for row in self.history(thread))

    def event(self, binding: str, source: str, event_id: str, marker: str):
        return self.http("POST", "/v1/events/" + binding, {
            "id": event_id, "source": source, "type": "request.seed",
            "data": {"marker": marker, "request_key": marker},
        }, self.source_tokens[source])

    def request_post(self, source: str, payload: dict):
        return self.http("POST", "/v1/requests", payload, self.source_tokens[source])

    def request_get(self, request_uuid: str, token: str | None = None):
        return self.http("GET", "/v1/requests/" + request_uuid, token=token)

    def request_update(self, request_uuid: str, source: str, payload: dict):
        return self.http("POST", "/v1/requests/" + request_uuid + "/updates", payload,
                         self.source_tokens[source])

    def cli_status(self, request_uuid: str, thread: str):
        return self.cli("request", "status", request_uuid, "--thread", thread)

    def run_protocol(self):
        code, receipt_a = self.event("request-a-binding", SOURCE_A, "seed-a", "seed-a")
        self.check("source_a_event_accepted", code == 202 and isinstance(receipt_a, dict), code=code)
        delivery_a = receipt_a.get("delivery_id")
        code, receipt_b = self.event("request-b-binding", SOURCE_B, "seed-b", "seed-b")
        self.check("source_b_event_accepted", code == 202 and isinstance(receipt_b, dict), code=code)
        delivery_b = receipt_b.get("delivery_id")
        if not delivery_a or not delivery_b:
            raise CanaryError("seed event did not return delivery IDs")
        self.wait_for(lambda: self.history_has(THREAD_A, "seed-a"), "source A seed consumption")
        self.wait_for(lambda: self.history_has(THREAD_B, "seed-b"), "source B seed consumption")

        tracked_a = self.cli("request", "track", delivery_a, "--key", "request-a",
                             "--thread", THREAD_A, "--summary", "Primary request")
        rid_a = request_id(tracked_a)
        revision_a = revision_of(tracked_a)
        self.check("track_starts_received_request_quietly",
                   bool(rid_a) and state_of(tracked_a) == "received" and revision_a is not None,
                   request_id=rid_a, revision=revision_a)
        quiet_a = len(self.history(THREAD_A))
        time.sleep(.5)
        self.check("track_received_is_quiet", len(self.history(THREAD_A)) == quiet_a)

        duplicate_track = self.cli("request", "track", delivery_a, "--key", "request-a",
                                   "--thread", THREAD_A, "--summary", "Primary request")
        self.check("track_reuses_stable_request_id", request_id(duplicate_track) == rid_a)
        get_a_code, get_a = self.request_get(rid_a, self.source_tokens[SOURCE_A])
        self.check("http_source_can_read_own_request", get_a_code == 200 and request_id(get_a) == rid_a)
        list_a_code, list_a = self.http("GET", "/v1/requests?thread=" + THREAD_A,
                                        token=self.source_tokens[SOURCE_A])
        self.check("http_list_is_exact_thread_and_source_scoped",
                   list_a_code == 200 and any(request_id(row) == rid_a for row in rows_of(list_a)))
        admin_get_code, _ = self.request_get(rid_a, self.admin_token)
        self.check("administrator_token_is_not_a_source_token", admin_get_code == 401)
        foreign_get_code, _ = self.request_get(rid_a, self.source_tokens[SOURCE_B])
        self.check("source_cannot_read_foreign_request", foreign_get_code in (403, 404))
        wrong_status_code, _, wrong_status_error = self.cli_raw("request", "status", rid_a, "--thread", THREAD_B)
        self.check("cli_thread_scope_rejects_wrong_conversation",
                   wrong_status_code != 0 and bool(wrong_status_error))
        list_b = self.cli("request", "list", "--thread", THREAD_B)
        self.check("request_list_is_conversation_scoped", not any(request_id(row) == rid_a for row in rows_of(list_b)))

        duplicate_http_code, duplicate_http = self.request_post(SOURCE_A, {
            "delivery_id": delivery_a, "request_key": "request-a",
            "payload": {"summary": "Primary request"},
        })
        self.check("http_duplicate_track_is_idempotent",
                   duplicate_http_code in (200, 201, 202) and request_id(duplicate_http) == rid_a)
        conflict_code, _ = self.request_post(SOURCE_A, {
            "delivery_id": delivery_a, "request_key": "request-a", "payload": {"same": False},
        })
        self.check("request_key_reuse_with_different_payload_rejected", conflict_code == 409)
        foreign_code, _ = self.request_post(SOURCE_B, {
            "delivery_id": delivery_a, "request_key": "foreign-a", "payload": {"foreign": True},
        })
        self.check("source_cannot_track_foreign_receipt", foreign_code in (403, 404))

        tracked_b_code, tracked_b = self.request_post(SOURCE_B, {
            "delivery_id": delivery_b, "request_key": "request-b", "payload": {"owner": SOURCE_B},
        })
        rid_b = request_id(tracked_b)
        self.check("source_b_tracks_own_receipt", tracked_b_code in (200, 201, 202) and bool(rid_b))

        if revision_a is None:
            raise CanaryError("tracked request did not expose a revision")
        ack_code, ack_value = self.request_update(rid_a, SOURCE_A, {
            "update_id": "ack-a-1", "state": "acknowledged", "expected_revision": revision_a,
            "detail": "acknowledged-a",
        })
        revision_ack = revision_of(ack_value)
        self.check("acknowledgement_is_accepted_and_quiet",
                   ack_code in (200, 202) and state_of(ack_value) == "acknowledged" and revision_ack is not None,
                   revision=revision_ack)
        history_after_ack = len(self.history(THREAD_A))
        duplicate_ack_code, duplicate_ack = self.request_update(rid_a, SOURCE_A, {
            "update_id": "ack-a-1", "state": "acknowledged", "expected_revision": revision_a,
            "detail": "acknowledged-a",
        })
        self.check("duplicate_acknowledgement_is_stable",
                   duplicate_ack_code in (200, 202) and state_of(duplicate_ack) == "acknowledged" and
                   len(self.history(THREAD_A)) == history_after_ack)

        if revision_ack is None:
            raise CanaryError("acknowledgement did not expose a revision")
        stale_code, _ = self.request_update(rid_a, SOURCE_A, {
            "update_id": "stale-a", "state": "in_progress", "expected_revision": revision_a,
            "detail": "stale-a",
        })
        self.check("stale_update_rejected_by_compare_and_set", stale_code == 409)
        marker_progress = "request-a-in-progress-marker"
        progress_code, progress_value = self.request_update(rid_a, SOURCE_A, {
            "update_id": "progress-a-1", "state": "in_progress", "expected_revision": revision_ack,
            "detail": marker_progress,
        })
        revision_progress = revision_of(progress_value)
        self.check("in_progress_update_is_accepted", progress_code in (200, 202) and state_of(progress_value) == "in_progress")
        self.wait_for(lambda: self.history_has(THREAD_A, marker_progress), "in-progress request event")
        self.check("meaningful_transition_emits_one_request_event", self.history_count(THREAD_A, marker_progress) == 1)
        duplicate_progress_code, _ = self.request_update(rid_a, SOURCE_A, {
            "update_id": "progress-a-1", "state": "in_progress", "expected_revision": revision_ack,
            "detail": marker_progress,
        })
        time.sleep(.5)
        self.check("duplicate_transition_does_not_duplicate_event",
                   duplicate_progress_code in (200, 202) and self.history_count(THREAD_A, marker_progress) == 1)

        if revision_progress is None:
            raise CanaryError("in-progress update did not expose a revision")
        marker_completed = "request-a-completed-marker"
        completed_code, completed_value = self.request_update(rid_a, SOURCE_A, {
            "update_id": "complete-a-1", "state": "completed", "expected_revision": revision_progress,
            "detail": marker_completed,
        })
        revision_completed = revision_of(completed_value)
        self.check("completed_transition_is_accepted", completed_code in (200, 202) and state_of(completed_value) == "completed")
        self.wait_for(lambda: self.history_has(THREAD_A, marker_completed), "completed request event")
        self.check("completed_event_is_emitted_once", self.history_count(THREAD_A, marker_completed) == 1)
        terminal_code, _ = self.request_update(rid_a, SOURCE_A, {
            "update_id": "late-failed-a", "state": "failed", "expected_revision": revision_completed,
            "detail": "late-failed-a",
        })
        self.check("terminal_state_rejects_later_failure", terminal_code == 409)

        marker_cancelled = "request-b-cancelled-marker"
        status_b_code, status_b = self.request_get(rid_b, self.source_tokens[SOURCE_B])
        self.check("source_b_can_read_own_request", status_b_code == 200)
        revision_b = revision_of(status_b)
        cancel_code, cancel_value = self.request_update(rid_b, SOURCE_B, {
            "update_id": "cancel-b-1", "state": "cancelled", "expected_revision": revision_b,
            "detail": marker_cancelled,
        })
        self.check("explicit_cancellation_is_accepted", cancel_code in (200, 202) and state_of(cancel_value) == "cancelled")
        self.wait_for(lambda: self.history_has(THREAD_B, marker_cancelled), "cancelled request event")
        self.check("cancellation_event_is_source_scoped", not self.history_has(THREAD_A, marker_cancelled))

        code_c, receipt_c = self.event("request-a-binding", SOURCE_A, "seed-c", "seed-c")
        if code_c != 202 or not isinstance(receipt_c, dict):
            raise CanaryError("source C seed event was not accepted")
        tracked_c = self.cli("request", "track", receipt_c["delivery_id"], "--key", "request-c",
                             "--thread", THREAD_A, "--summary", "Restart request")
        rid_c = request_id(tracked_c)
        revision_c = revision_of(tracked_c)
        progress_c_code, progress_c = self.request_update(rid_c, SOURCE_A, {
            "update_id": "progress-c-1", "state": "in_progress", "expected_revision": revision_c,
            "detail": "request-c-in-progress-marker",
        })
        self.check("restart_fixture_request_reaches_in_progress",
                   progress_c_code in (200, 202) and state_of(progress_c) == "in_progress")
        self.wait_for(lambda: self.history_has(THREAD_A, "request-c-in-progress-marker"), "restart request event")
        self.stop_receiver(kill=True)
        self.start_receiver()
        persisted_code, persisted = self.request_get(rid_c, self.source_tokens[SOURCE_A])
        self.check("request_state_survives_receiver_sigkill_restart",
                   persisted_code == 200 and state_of(persisted) == "in_progress")
        duplicate_c_code, _ = self.request_update(rid_c, SOURCE_A, {
            "update_id": "progress-c-1", "state": "in_progress", "expected_revision": revision_c,
            "detail": "request-c-in-progress-marker",
        })
        time.sleep(.5)
        self.check("restart_does_not_duplicate_update_event", duplicate_c_code in (200, 202) and
                   self.history_count(THREAD_A, "request-c-in-progress-marker") == 1)

        code_d, receipt_d = self.event("request-a-binding", SOURCE_A, "seed-d", "seed-d")
        if code_d != 202 or not isinstance(receipt_d, dict):
            raise CanaryError("expiry seed event was not accepted")
        tracked_d = self.cli("request", "track", receipt_d["delivery_id"], "--key", "request-d",
                             "--thread", THREAD_A, "--summary", "Expiry request", "--expires-in", "1")
        rid_d = request_id(tracked_d)
        def expired_value():
            code, value = self.request_get(rid_d, self.source_tokens[SOURCE_A])
            return value if code == 200 and state_of(value) == "expired" else None

        expired = self.wait_for(expired_value, "request expiry", timeout=max(self.args.timeout, 5))
        self.check("expired_request_reaches_terminal_state", state_of(expired) == "expired")

        current_revision_c = revision_of(progress_c)
        if current_revision_c is None:
            raise CanaryError("restart fixture update did not expose a current revision")
        bad_code, _, bad_error = self.cli_raw("request", "update", rid_c, "--state", "not-a-state",
                                              "--update-id", "bad-state", "--revision", str(current_revision_c),
                                              "--thread", THREAD_A)
        self.check("cli_rejects_invalid_state_helpfully", bad_code != 0 and "state" in bad_error.lower())
        self.report["request_ids"] = {"completed": rid_a, "cancelled": rid_b, "restarted": rid_c, "expired": rid_d}

    def run_tui(self):
        tui_report = {
            "result": "FAIL",
            "surface": "ordinary Codex TUI in owned disposable PTY",
            "evidence_kind": "native history and pyte terminal evidence; no prompt-echo-only claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
        }
        terminal = receiver = rpc = None
        receiver_log = None
        try:
            import importlib.util
            helper_spec = importlib.util.spec_from_file_location("request_tui_helpers", ROOT / "scripts" / "tui-canary.py")
            if helper_spec is None or helper_spec.loader is None:
                raise CanaryError("cannot load owned PTY helper")
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
            Terminal = helper.Terminal
            from codex_monitor.session import Rpc

            work = Path(tempfile.mkdtemp(prefix="codex-monitor-request-tui-", dir="/tmp"))
            state = work / "state"
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
                return json.loads(process.stdout) if process.stdout.strip() else None

            tui_cli("init", "--port", str(port))
            admin = (state / "admin.token").read_text().strip()
            instruction = (
                "This is an isolated owned-PTY acceptance test. Do not use tools or read files. "
                "Reply exactly READY_REQUEST_TUI to this initial prompt. For the later user marker "
                "REQUEST_TUI_FOLLOWUP, reply exactly ACK_REQUEST_TUI_FOLLOWUP."
            )
            tui_argv = [
                "codex", "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never",
                "-c", f"model_reasoning_effort=\"{self.args.reasoning_effort}\"",
                "-c", "tui.animations=false", instruction,
            ]
            if self.args.model:
                tui_argv[1:1] = ["--model", self.args.model]
            terminal = Terminal(tui_argv)
            rpc = Rpc("shared-local")
            trust_sent = False
            thread = None

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

            def turns():
                return rpc.call("thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 100})["data"] if thread else []

            def native_has(kind, marker):
                for turn in turns():
                    for item in turn.get("items", []):
                        if item.get("type") != kind:
                            continue
                        text = item.get("text", "") if kind == "agentMessage" else json.dumps(item.get("content", []), ensure_ascii=False)
                        if marker in text:
                            return True
                return False

            def native_count(kind, marker):
                count = 0
                for turn in turns():
                    for item in turn.get("items", []):
                        if item.get("type") != kind:
                            continue
                        text = item.get("text", "") if kind == "agentMessage" else json.dumps(item.get("content", []), ensure_ascii=False)
                        count += marker in text
                return count

            def find_thread():
                nonlocal thread
                rows = rpc.call("thread/list", {"cwd": str(work), "limit": 10, "sourceKinds": ["cli"]})["data"]
                if len(rows) == 1:
                    thread = rows[0]["id"]
                return thread

            pump_until(find_thread, "ordinary TUI thread discovery")
            pump_until(lambda: native_has("agentMessage", "READY_REQUEST_TUI"), "initial native TUI response")
            source_result = tui_cli("source", "tui-source")
            source_token = Path(source_result["token_file"]).read_text().strip()
            tui_cli("bind", "request-tui-binding", "--thread", thread, "--source", "tui-source", "--endpoint", "shared-local")
            receiver_log = (work / "serve.log").open("w")
            receiver = subprocess.Popen(
                [str(self.args.python), "-m", "codex_monitor", "--state", str(state), "serve"],
                cwd=work, env=env, stdout=receiver_log, stderr=subprocess.STDOUT, start_new_session=True,
            )

            def status_ready():
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/status", headers={"Authorization": "Bearer " + admin})
                    with urllib.request.urlopen(request, timeout=3) as response:
                        value = json.load(response)
                    return value.get("worker_error") is None
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    return False

            pump_until(status_ready, "TUI receiver readiness", timeout=30)
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/events/request-tui-binding",
                data=json.dumps({"id": "tui-seed", "source": "tui-source", "type": "request.seed", "data": {"marker": "tui-seed"}}).encode(),
                headers={"Authorization": "Bearer " + source_token, "Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                receipt = json.load(response)
            delivery_id = receipt["delivery_id"]
            tracked = tui_cli("request", "track", delivery_id, "--key", "request-tui", "--thread", thread, "--summary", "TUI request")
            rid = request_id(tracked)
            revision = revision_of(tracked)

            def request_update(update_id, state, expected_revision, marker):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/requests/{rid}/updates",
                    data=json.dumps({
                        "update_id": update_id,
                        "state": state,
                        "expected_revision": expected_revision,
                        "detail": marker,
                    }).encode(),
                    headers={"Authorization": "Bearer " + source_token,
                             "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)

            def request_status():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/requests/{rid}",
                    headers={"Authorization": "Bearer " + source_token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)

            if not rid or revision is None:
                raise CanaryError("TUI request tracking did not return an ID and revision")
            acknowledged_marker = "request-tui-acknowledged-marker"
            _, acknowledged = request_update("tui-ack-1", "acknowledged", revision, acknowledged_marker)
            history_after_ack = native_count("userMessage", acknowledged_marker)
            time.sleep(.5)
            self.check(
                "tui_acknowledged_transition_is_quiet",
                state_of(acknowledged) == "acknowledged" and revision_of(acknowledged) == revision + 1
                and not native_has("userMessage", acknowledged_marker)
                and native_count("userMessage", acknowledged_marker) == history_after_ack,
            )
            tui_report["acknowledged_quiet"] = True
            revision = revision_of(acknowledged)

            draft_marker = "REQUEST_TUI_UNSENT_DRAFT"
            terminal.input(draft_marker)
            for _ in range(5):
                terminal.pump(.1)
            if draft_marker not in terminal.text():
                raise CanaryError("TUI draft did not render before request progress")
            marker = "request-tui-in-progress-marker"
            _, progress = request_update("tui-progress-1", "in_progress", revision, marker)
            revision = revision_of(progress)
            if state_of(progress) != "in_progress" or revision is None:
                raise CanaryError("TUI in-progress update did not return the expected state")
            pump_until(lambda: marker in terminal.text() and draft_marker in terminal.text(),
                       "request event rendered while preserving ordinary TUI draft")
            pump_until(lambda: native_has("userMessage", marker), "request event consumed in native history", timeout=30)
            _, persisted_progress = request_status()
            self.check(
                "tui_progress_consumption_preserves_draft_and_request_state",
                state_of(persisted_progress) == "in_progress"
                and draft_marker in terminal.text()
                and native_has("userMessage", marker)
                and native_count("userMessage", marker) == 1,
            )
            tui_report.update(draft_preserved=True, event_consumed=True,
                              request_state_after_event=state_of(persisted_progress))
            self.report["checks"]["tui_progress_consumption_preserves_draft_and_request_state"] = True

            # Clear the preserved draft explicitly so the next user turn is a
            # fresh follow-up.  Ctrl-U only edits the composer; it does not
            # create a native turn.
            terminal.input("\x15")
            for _ in range(5):
                terminal.pump(.1)
            if draft_marker in terminal.text():
                raise CanaryError("TUI could not clear the preserved draft before follow-up")

            completed_marker = "request-tui-completed-marker"
            _, completed = request_update("tui-complete-1", "completed", revision, completed_marker)
            if state_of(completed) != "completed":
                raise CanaryError("TUI completed update did not return completed state")
            pump_until(lambda: completed_marker in terminal.text(),
                       "completed request event rendered in ordinary TUI")
            pump_until(lambda: native_has("userMessage", completed_marker),
                       "completed request event consumed in native history", timeout=30)
            self.check(
                "tui_completed_notification_is_consumed_once",
                native_count("userMessage", completed_marker) == 1,
            )
            tui_report["completed_event_consumed"] = True
            self.report["checks"]["tui_completed_notification_is_consumed_once"] = True

            terminal.input("REQUEST_TUI_FOLLOWUP")
            for _ in range(5):
                terminal.pump(.1)
            terminal.input("\r")
            pump_until(lambda: native_has("agentMessage", "ACK_REQUEST_TUI_FOLLOWUP"), "ordinary TUI follow-up response", timeout=60)
            tui_report.update(result="PASS", client_ui_verified=True, event_consumed=True,
                              followup_response_seen=True, thread=thread, request_id=rid,
                              process_model={"tui_pid": terminal.pid, "receiver_pid": receiver.pid})
            self.report["checks"]["ordinary_tui_renders_and_consumes_request_event"] = True
            self.report["checks"]["ordinary_tui_followup_response_after_request_lifecycle"] = True
            self.step("ordinary_tui_request_lifecycle_rendered", thread=thread, request_id=rid)
        except ModuleNotFoundError as exc:
            if exc.name == "pyte":
                tui_report.update(result="SKIPPED", reason="optional PTY dependency pyte is not installed")
                self.step("ordinary_tui_skipped", reason=tui_report["reason"])
                return
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
            self.report["tui"] = tui_report

    def cleanup(self):
        self.stop_receiver(kill=True)
        if self.receiver_log:
            self.receiver_log.close()
            self.receiver_log = None
        if self.work is not None:
            self.report["temporary_work"] = str(self.work)
            try:
                import shutil
                shutil.rmtree(self.work)
                self.report["temporary_work_removed"] = True
            except OSError as exc:
                self.report["temporary_work_removed"] = False
                self.report["cleanup_error"] = str(exc)

    def run(self):
        try:
            self.setup()
            self.run_protocol()
            if self.args.tui:
                self.run_tui()
                if self.report.get("tui", {}).get("result") != "PASS":
                    self.report["result"] = "INCOMPLETE"
                    self.step("request_lifecycle_canary_incomplete", result="INCOMPLETE",
                              tui_result=self.report.get("tui", {}).get("result"))
                    return
            self.report["result"] = "PASS"
            self.step("request_lifecycle_canary_completed", result="PASS")
        finally:
            self.cleanup()
            self.report["finished_at"] = now()
            self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
                        help="explicitly opt in to the request lifecycle canary")
    parser.add_argument("--python", required=True, type=Path,
                        help="absolute Python executable from the installed wheel")
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--tui", action="store_true",
                        help="also run the separate ordinary Codex TUI check")
    parser.add_argument("--model", help="optional model override for the owned ordinary TUI")
    parser.add_argument("--reasoning-effort",
                        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                        default="low", help="TUI reasoning effort (default: low)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.python.is_absolute() or not args.python.is_file() or not os.access(args.python, os.X_OK):
        parser.error("--python must be an absolute executable installed-runtime Python")
    canary = Canary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        canary.report["steps"].append({"name": "request_lifecycle_canary_failed",
                                       "elapsed_seconds": round(time.monotonic() - canary.started, 3),
                                       "at": now(), "error": canary.report["error"]})
        canary.report["finished_at"] = now()
        canary.save()
        print(json.dumps({"result": "FAIL", "report": str(args.report), "error": canary.report["error"]}), flush=True)
        return 1
    print(json.dumps({"result": canary.report["result"], "report": str(args.report)}), flush=True)
    return 0 if canary.report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
