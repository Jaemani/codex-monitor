#!/usr/bin/env python3
"""Verify the managed receiver through one disposable macOS LaunchAgent.

The canary uses the final installed wheel as the receiver runtime.  Its
``codex`` executable is a temporary protocol peer backed by
``tests/fake_app_server.py``; this demonstrates queue acceptance and history
consumption without claiming model or UI behavior.  The only user-level
launchd object created is the state-derived label returned by ``service``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fake_app_server.py"
THREAD = "thread-user"
WATCH = "managed-service-watch"


class CanaryError(RuntimeError):
    pass


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def stable_registry(value: dict) -> dict:
    return {
        key: value.get(key)
        for key in ("id", "thread", "name", "file", "interval", "binding", "enabled")
    }


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report = {
            "result": "FAIL",
            "scope": "installed wheel / macOS LaunchAgent / managed file monitor",
            "evidence_kind": "fake App Server queue and history evidence; no model or UI claim",
            "client_ui_verified": False,
            "model_delivery_verified": False,
            "checks": {},
            "steps": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "python": str(args.python),
            "thread": THREAD,
            "watch": WATCH,
        }
        if args.wheel_sha256:
            self.report["wheel_sha256"] = args.wheel_sha256
        self.work: Path | None = None
        self.state: Path | None = None
        self.file: Path | None = None
        self.fake_state: Path | None = None
        self.fake_bin: Path | None = None
        self.env: dict[str, str] | None = None
        self.port: int | None = None
        self.admin_token: str | None = None
        self.service: dict | None = None
        self.service_installed = False

    def save_report(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    def step(self, name: str, **details):
        item = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        self.report["steps"].append(item)
        self.save_report()
        print(json.dumps({"progress": item}), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise CanaryError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def cli(self, *argv: str, expected: int = 0, timeout: float = 30):
        if self.state is None or self.work is None or self.env is None:
            raise CanaryError("canary is not initialized")
        command = [
            str(self.args.python), "-m", "codex_monitor", "--state", str(self.state), *argv
        ]
        process = subprocess.run(
            command,
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
        if not process.stdout.strip():
            return None
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CanaryError(f"CLI did not return JSON: {process.stdout[:1000]!r}") from exc

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
            time.sleep(.2)
        detail = f": {last_error}" if last_error else ""
        raise CanaryError(f"{label} timed out{detail}")

    def setup(self):
        self.work = Path(tempfile.mkdtemp(prefix="codex-monitor-service-", dir="/tmp"))
        self.state = self.work / "state"
        self.file = self.work / "watched.txt"
        self.fake_state = self.work / "fake-app-server.json"
        self.file.write_text("managed baseline\n")
        (self.work / "codex-home").mkdir(mode=0o700)
        self.fake_bin = self.work / "fake-bin"
        self.fake_bin.mkdir(mode=0o700)
        launcher = self.fake_bin / "codex"
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(self.args.python))} "
            f"{shlex.quote(str(FAKE_APP_SERVER))} {shlex.quote(str(self.fake_state))}\n"
        )
        launcher.chmod(0o700)

        env = os.environ.copy()
        for key in (
            "PYTHONPATH",
            "CODEX_THREAD_ID",
            "CODEX_MONITOR_HOME",
            "CODEX_SQLITE_HOME",
            "CODEX_MONITOR_SERVER_TOKEN",
            "CODEX_MONITOR_SERVER_TOKEN_FILE",
        ):
            env.pop(key, None)
        env["CODEX_HOME"] = str(self.work / "codex-home")
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        self.env = env

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
        self.report["fake_app_server"] = str(FAKE_APP_SERVER)
        self.report["private_paths"] = {
            "work": str(self.work),
            "state": str(self.state),
            "codex_home": str(self.work / "codex-home"),
            "fake_bin": str(self.fake_bin),
        }
        self.cli("init", "--port", str(self.port))
        self.admin_token = (self.state / "admin.token").read_text().strip()
        self.cli("monitor", "create", WATCH, "--file", str(self.file), "--thread", THREAD,
                 "--interval", "0.2", "--endpoint", "shared-local")
        self.step("installed_runtime_initialized", port=self.port, state=str(self.state))

    def http_status(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/status",
            headers={"Authorization": "Bearer " + str(self.admin_token)},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def service_ready(self):
        try:
            http = self.http_status()
            status = self.cli("status")
            return http if status.get("receiver_alive") and http.get("worker_error") is None else None
        except (OSError, urllib.error.URLError, json.JSONDecodeError, CanaryError):
            return None

    def service_pid(self):
        if not self.service:
            return None
        target = self.service["domain"] + "/" + self.service["label"]
        result = subprocess.run(
            ["launchctl", "print", target], capture_output=True, text=True, timeout=10
        )
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return int(match.group(1)) if match else None

    def atomic_replace(self, text: str):
        if self.file is None:
            raise CanaryError("watched file is not initialized")
        temporary = self.file.with_name("." + self.file.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.file)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def watch_status(self):
        return self.cli("monitor", "status", WATCH, "--thread", THREAD)

    def fake_history(self):
        if self.fake_state is None or not self.fake_state.exists():
            return []
        for _ in range(30):
            try:
                value = json.loads(self.fake_state.read_text())
                return value.get("history", []) if isinstance(value, dict) else []
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.03)
        raise CanaryError("fake App Server state is unavailable or malformed")

    def accepted_receipt(self, expected_hash: str, prior_ids: set[str]):
        status = self.watch_status()
        receipt = status.get("last_delivery") or {}
        delivery_id = receipt.get("delivery_id")
        if not delivery_id or delivery_id in prior_ids or receipt.get("state") != "accepted":
            return None
        event = self.cli("event", delivery_id)
        if event.get("state") != "accepted":
            return None
        rows = [
            row for row in self.fake_history()
            if row.get("threadId") == THREAD and row.get("clientId") == event.get("client_id")
        ]
        rendered = " ".join(
            item.get("text", "")
            for row in rows
            for item in row.get("content", [])
            if isinstance(item, dict)
        )
        if len(rows) != 1 or expected_hash not in rendered:
            return None
        return {"id": delivery_id, "event": event, "history_rows": rows}

    def snapshot(self):
        if self.state is None:
            raise CanaryError("state is not initialized")
        status = self.watch_status()
        checkpoint = self.state / "managed" / (status["id"] + ".json")
        events = [
            self.cli("event", delivery_id)
            for delivery_id in self.report.get("delivery_ids", [])
        ]
        return {
            "registry": stable_registry(status),
            "checkpoint_sha256": digest(checkpoint) if checkpoint.exists() else None,
            "sqlite_exists": (self.state / "monitor.sqlite3").exists(),
            "sqlite_sha256": digest(self.state / "monitor.sqlite3") if (self.state / "monitor.sqlite3").exists() else None,
            "events": events,
        }

    def run(self):
        self.setup()
        self.check("init_and_explicit_monitor_create", self.watch_status()["thread"] == THREAD)

        self.service = self.cli("service", "install")
        self.service_installed = True
        self.report["launch_agent"] = {
            "label": self.service["label"],
            "domain": self.service["domain"],
            "plist": self.service["plist"],
        }
        self.wait_for(self.service_ready, "LaunchAgent HTTP and receiver readiness")
        first_pid = self.wait_for(self.service_pid, "LaunchAgent process")
        self.check("launchagent_installed_started_and_ready", self.cli("status")["receiver_alive"])

        baseline_hash = digest(self.file)
        self.wait_for(
            lambda: (self.watch_status().get("last_sample") or {}).get("sha256") == baseline_hash,
            "initial managed baseline",
        )
        self.check("initial_sample_creates_silent_baseline", not self.fake_history() and
                   self.watch_status().get("last_delivery") is None)

        self.atomic_replace("managed change one\n")
        hash_one = digest(self.file)
        first = self.wait_for(
            lambda: self.accepted_receipt(hash_one, set()),
            "first accepted and consumed managed receipt",
        )
        self.report["delivery_ids"] = [first["id"]]
        self.check("atomic_change_has_one_accepted_consumed_receipt", len(self.fake_history()) == 1,
                   delivery_id=first["id"], sha256=hash_one)
        first_snapshot = self.snapshot()
        first_pid = self.service_pid()

        target = self.service["domain"] + "/" + self.service["label"]
        subprocess.run(["launchctl", "kill", "SIGKILL", target], check=True,
                       capture_output=True, timeout=10)

        def find_restarted_pid():
            value = self.service_pid()
            return value if value and value != first_pid else None

        restarted_pid = self.wait_for(
            find_restarted_pid,
            "LaunchAgent restart after SIGKILL",
            timeout=max(30, self.args.timeout),
        )
        self.wait_for(self.service_ready, "receiver readiness after SIGKILL")
        after_restart = self.watch_status()
        self.check("sigkill_restart_preserves_checkpoint_and_receipt",
                   (after_restart.get("last_sample") or {}).get("sha256") == hash_one and
                   after_restart.get("last_delivery", {}).get("delivery_id") == first["id"] and
                   after_restart.get("last_delivery", {}).get("state") == "accepted" and
                   len(self.fake_history()) == 1,
                   old_pid=first_pid, new_pid=restarted_pid, delivery_id=first["id"])

        self.atomic_replace("managed change two\n")
        hash_two = digest(self.file)
        second = self.wait_for(
            lambda: self.accepted_receipt(hash_two, {first["id"]}),
            "second accepted and consumed managed receipt",
        )
        self.report["delivery_ids"].append(second["id"])
        self.check("second_change_has_distinct_one_receipt", second["id"] != first["id"] and
                   len(self.fake_history()) == 2, delivery_id=second["id"], sha256=hash_two)

        before_stop = self.snapshot()
        stopped = self.cli("service", "stop")
        self.check("service_stop_unloads_owned_job",
                   stopped["loaded"] is False and self.cli("status")["receiver_alive"] is False)
        self.check("stop_preserves_registry_sqlite_checkpoint_and_receipts",
                   self._preserved(before_stop), registry=before_stop["registry"])

        uninstalled = self.cli("service", "uninstall")
        self.service_installed = False
        self.check("uninstall_removes_owned_plist_and_job",
                   uninstalled["installed"] is False and not Path(self.service["plist"]).exists())
        after_uninstall = self.snapshot()
        self.check("uninstall_preserves_registry_sqlite_checkpoint_and_receipts",
                   self._preserved(before_stop, after_uninstall), registry=after_uninstall["registry"])
        self.report["persistence"] = {
            "before_stop": before_stop,
            "after_uninstall": after_uninstall,
            "sqlite_sha256_changed_on_stop": before_stop["sqlite_sha256"] != after_uninstall["sqlite_sha256"],
        }
        self.report["result"] = "PASS"
        self.step("managed_service_canary_completed", result="PASS", pid_before=first_pid,
                   pid_after_sigkill=restarted_pid)

    def _preserved(self, before: dict, after: dict | None = None) -> bool:
        current = self.snapshot() if after is None else after
        if before["registry"] != current["registry"]:
            return False
        if not current["sqlite_exists"] or not before["sqlite_exists"]:
            return False
        if before["checkpoint_sha256"] != current["checkpoint_sha256"]:
            return False
        before_events = {row["id"]: row for row in before["events"]}
        after_events = {row["id"]: row for row in current["events"]}
        return all(
            delivery_id in after_events and
            after_events[delivery_id]["state"] == before_row["state"] and
            after_events[delivery_id]["client_id"] == before_row["client_id"]
            for delivery_id, before_row in before_events.items()
        )

    def cleanup(self):
        cleanup_error = None
        if self.service_installed and self.state is not None and self.env is not None:
            try:
                status = self.cli("service", "status")
                if status.get("installed"):
                    self.cli("service", "uninstall")
                self.service_installed = False
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
        if cleanup_error:
            self.report["cleanup_error"] = cleanup_error
            if self.work:
                self.report["retained_state"] = str(self.work)
            return
        if self.work:
            self.report["temporary_work"] = str(self.work)
            shutil.rmtree(self.work, ignore_errors=True)
            self.report["temporary_work_removed"] = not self.work.exists()

    def finish(self):
        self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.save_report()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if not args.python.is_absolute() or not args.python.is_file() or not os.access(args.python, os.X_OK):
        parser.error("--python must be an absolute executable installed-runtime Python")
    if args.timeout < 10:
        parser.error("--timeout must be at least 10 seconds")
    if args.wheel_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.wheel_sha256):
        parser.error("--wheel-sha256 must be a 64-character hexadecimal digest")
    canary = Canary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            canary.step("managed_service_canary_failed", error=canary.report["error"])
        except Exception:
            pass
    finally:
        canary.cleanup()
        canary.finish()
    return 0 if canary.report.get("result") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
