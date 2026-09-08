#!/usr/bin/env python3
"""Opt-in real launchd lifecycle test, confined to one disposable state/label."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


@contextmanager
def disposable_state(report):
    temporary = tempfile.mkdtemp(prefix="cm-launchd-", dir="/tmp")
    try:
        yield temporary
    finally:
        if "cleanup_error" not in report:
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--python", required=True, type=Path, help="absolute clean wheel-installed Python")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--thread", help="optional explicitly authorized live Desktop/CLI test thread")
    args = parser.parse_args()
    if not args.python.is_absolute():
        parser.error("--python must be absolute")
    report = {"result": "FAIL", "scope": "actual macOS launchd + installed receiver + HTTP", "checks": {}}
    started = time.monotonic()
    with disposable_state(report) as temporary:
        state = Path(temporary) / "state"
        cmd = [str(args.python), "-m", "codex_monitor", "--state", str(state)]
        installed = False

        def cli(*argv):
            result = subprocess.run(cmd + list(argv), cwd=temporary, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f"{argv[0]}: {result.stderr.strip()[:1500]}")
            return json.loads(result.stdout)

        def wait_for(fn, timeout=30):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                value = fn()
                if value:
                    return value
                time.sleep(.25)
            raise TimeoutError("service condition timed out")

        def check(name, condition):
            if not condition:
                raise AssertionError(name)
            report["checks"][name] = True
            print(json.dumps({"check": name, "elapsed": round(time.monotonic() - started, 2)}), flush=True)

        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            cli("init", "--port", str(port))
            cli("source", "test")
            cli("bind", "service-test", "--thread", args.thread or "ack-only", "--source", "test")
            paths = [state / "admin.token", state / "source-test.token", state / "config.json"]
            original = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
            source_token = (state / "source-test.token").read_text().strip()
            admin_token = (state / "admin.token").read_text().strip()

            def request(path, data=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}" + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": "Bearer " + (source_token if data is not None else admin_token),
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3) as response:
                    return json.load(response)

            def healthy():
                try:
                    value = request("/v1/status")
                    return value if value.get("worker_error") is None else None
                except (OSError, urllib.error.URLError):
                    return None

            installed = True  # Also clean up a partially failed install.
            service = cli("service", "install")
            report["label"] = service["label"]
            target = service["domain"] + "/" + service["label"]

            def pid():
                result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=10)
                match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.M)
                return int(match[1]) if match else None

            wait_for(healthy)
            initial_pid = wait_for(pid)
            check("installed_receiver_http_ready", cli("status")["receiver_alive"])
            event = {"id": "retained-ack", "source": "test", "type": "agent.ack", "data": {}}
            if args.thread:
                marker = "MONITOR_SERVICE_EVENT_" + uuid.uuid4().hex[:12]
                event = {"id": marker, "source": "test", "type": "monitor.canary", "data": {
                    "marker": marker,
                    "instruction": "Authorized installed receiver service test for this ongoing codex-monitor task. "
                    "Record receipt of this marker visibly and continue the existing implementation and validation. "
                    "No permissions or task scope are changed by this event."}}
                report.update(thread=args.thread, marker=marker, desktop_response_verified=False)
            receipt = request("/v1/events/service-test", event)
            check("authenticated_http_receipt", bool(receipt["delivery_id"]))
            if args.thread:
                def accepted():
                    item = cli("event", receipt["delivery_id"])
                    if item["state"] in ("dead", "uncertain"):
                        raise RuntimeError(item["error"])
                    return item if item["state"] == "accepted" else None
                value = wait_for(accepted)
                report.update(client_id=value["client_id"], submission_id=value["submission_id"])
                check("installed_service_delivers_to_native_queue", True)
            cli("service", "start")
            check("start_is_idempotent", pid() == initial_pid)
            subprocess.run(["launchctl", "kill", "SIGKILL", target], check=True, capture_output=True, timeout=10)
            wait_for(lambda: (value := pid()) and value != initial_pid)
            wait_for(healthy)
            check("launchd_recovers_killed_receiver", cli("status")["receiver_alive"])
            duplicate = request("/v1/events/service-test", event)
            check("crash_preserves_dedup", duplicate["duplicate"] and duplicate["delivery_id"] == receipt["delivery_id"])
            previous_pid = pid()
            cli("service", "restart")
            wait_for(lambda: (value := pid()) and value != previous_pid)
            wait_for(healthy)
            check("explicit_restart_recovers_http", cli("status")["receiver_alive"])
            cli("service", "stop")
            wait_for(lambda: not cli("status")["receiver_alive"])
            check("stop_unloads_job", not cli("service", "status")["loaded"])
            cli("service", "start")
            wait_for(healthy)
            cli("service", "install")
            wait_for(healthy)
            check("reinstall_preserves_credentials_and_config", original == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
            cli("service", "uninstall")
            installed = False
            wait_for(lambda: not cli("status")["receiver_alive"])
            check("uninstall_removes_only_owned_service", not Path(service["plist"]).exists() and all(p.exists() for p in paths))
            check("uninstall_preserves_receipt", cli("event", receipt["delivery_id"])["state"] == ("accepted" if args.thread else "ignored"))
            report["result"] = "PASS"
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if installed:
                try:
                    cli("service", "uninstall")
                except Exception as exc:
                    report["cleanup_error"] = str(exc)
                    # Never remove the state beneath a service that may still run.
                    # The caller gets the exact path needed for manual recovery.
                    report["retained_state"] = str(state)
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
