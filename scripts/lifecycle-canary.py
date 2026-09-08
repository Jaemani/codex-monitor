#!/usr/bin/env python3
"""Run with a clean wheel-installed Python, outside the source checkout.

Exercises real receiver processes and HTTP during an unavailable App Server.
No model or UI claim: recovery means durable operator-visible state, not delivery.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import codex_monitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--outage-seconds", type=int, default=3600)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.outage_seconds < 30:
        parser.error("outage must last at least 30 seconds")
    source = Path(__file__).resolve().parents[1]
    if Path(codex_monitor.__file__).resolve().is_relative_to(source):
        parser.error("use a clean wheel-installed Python; editable/source imports are not acceptance")
    started = time.monotonic()
    report = {"result": "FAIL", "scope": "installed receiver / real HTTP / unavailable endpoint",
              "started_at": datetime.now(timezone.utc).isoformat(),
              "version": importlib.metadata.version("codex-monitor"),
              "python": sys.version.split()[0], "outage_seconds_requested": args.outage_seconds,
              "client_ui_verified": False, "model_delivery_verified": False, "checks": {}}
    processes = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"][name] = True
        print(json.dumps({"check": name, "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)

    try:
        with tempfile.TemporaryDirectory(prefix="codex-monitor-lifecycle-") as tmp:
            root = Path(tmp)
            state = root / "state"
            # Keep the destination port reserved but not listening for the whole run.
            with socket.socket() as unavailable, socket.socket() as reservation:
                unavailable.bind(("127.0.0.1", 0))
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
                reservation.close()
                endpoint = f"ws://127.0.0.1:{unavailable.getsockname()[1]}"
                command = [sys.executable, "-m", "codex_monitor", "--state", str(state)]

                def cli(*argv, expected=0):
                    result = subprocess.run(command + list(argv), cwd=root, capture_output=True, text=True, timeout=20)
                    if result.returncode != expected:
                        raise RuntimeError(f"CLI {argv[0]} exited {result.returncode}, expected {expected}")
                    return json.loads(result.stdout) if result.stdout.strip() else None

                def wait_for(fn, timeout=25):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        result = fn()
                        if result:
                            return result
                        time.sleep(.2)
                    raise TimeoutError("receiver condition timed out")

                cli("init", "--port", str(port))
                cli("source", "test")
                cli("bind", "fault", "--thread", "unavailable-test-thread", "--source", "test", "--endpoint", endpoint)
                cli("bind", "healthy", "--thread", "ack-only-test-thread", "--source", "test", "--endpoint", endpoint)
                tokens = [state / "admin.token", state / "source-test.token"]
                fingerprints = [hashlib.sha256(p.read_bytes()).hexdigest() for p in tokens]
                cli("init", expected=2)
                check("reinit_preserves_credentials", fingerprints == [hashlib.sha256(p.read_bytes()).hexdigest() for p in tokens])
                check("private_permissions", state.stat().st_mode & 0o777 == 0o700 and
                      all(p.stat().st_mode & 0o777 == 0o600 for p in tokens + [state / "config.json", state / "monitor.sqlite3"]))
                admin, token = [p.read_text().strip() for p in tokens]
                url = f"http://127.0.0.1:{port}"

                def http(path, data=None):
                    request = urllib.request.Request(url + path,
                        data=json.dumps(data).encode() if data is not None else None,
                        headers={"Authorization": "Bearer " + (token if data is not None else admin),
                                 "Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)

                def live():
                    try:
                        return http("/v1/status")
                    except (OSError, urllib.error.URLError):
                        return None

                def start():
                    p = subprocess.Popen(command + ["serve"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    processes.append(p)
                    wait_for(live)
                    if p.poll() is not None:
                        raise RuntimeError("new receiver exited before readiness")
                    return p

                def stop(p, kill=False):
                    p.kill() if kill else p.terminate()
                    p.wait(timeout=20)

                receiver = start()
                cli("serve", expected=2)
                check("second_receiver_rejected", receiver.poll() is None)
                event = {"id": "retained-fault", "source": "test", "type": "test.event", "data": {"marker": "FAULT"}}
                receipt = http("/v1/events/fault", event)
                delivery = receipt["delivery_id"]
                stop(receiver, kill=True)
                check("sigkill_releases_process", receiver.returncode < 0)
                receiver = start()
                duplicate = http("/v1/events/fault", event)
                check("sigkill_preserves_receipt_and_dedup", duplicate["duplicate"] and duplicate["delivery_id"] == delivery)
                # A kill between durable submitting and result persistence can leave uncertain.
                # Either outcome is correct; neither may be silently accepted or discarded.
                def settled():
                    value = cli("event", delivery)
                    report["last_delivery"] = {key: value[key] for key in ("state", "attempts", "error")}
                    return value["state"] in ("dead", "uncertain")
                # Five attempts can each spend the transport's 10-second timeout,
                # in addition to exponential backoff; don't assume instant refusal.
                wait_for(settled, timeout=90)
                initial = cli("event", delivery)
                retained_states = {"dead"} if initial["state"] == "dead" else {"uncertain", "submitting"}
                check("outage_visible_without_false_acceptance", initial["state"] in ("dead", "uncertain"))
                outage_started = time.monotonic()
                samples = 0
                restarts = 0
                next_restart = outage_started + min(60, args.outage_seconds / 2)
                while time.monotonic() - outage_started < args.outage_seconds:
                    status = http("/v1/status")
                    if status["worker_error"] is not None or receiver.poll() is not None:
                        raise AssertionError("receiver unhealthy during outage")
                    current = cli("event", delivery)
                    if current["state"] not in retained_states or current["attempts"] != initial["attempts"]:
                        raise AssertionError("terminal/ambiguous delivery was silently retried")
                    ack = http("/v1/events/healthy", {"id": f"ack-{samples}", "source": "test", "type": "agent.ack", "data": {}})
                    if cli("event", ack["delivery_id"])["state"] != "ignored":
                        raise AssertionError("ACK woke the model or other ingress stopped")
                    samples += 1
                    if time.monotonic() >= next_restart:
                        stop(receiver)
                        receiver = start()
                        restarts += 1
                        next_restart = time.monotonic() + 60
                        print(json.dumps({"outage_elapsed": round(time.monotonic() - outage_started), "restarts": restarts}), flush=True)
                    time.sleep(min(5, max(0, args.outage_seconds - (time.monotonic() - outage_started))))
                report.update(outage_seconds_observed=round(time.monotonic() - outage_started, 2),
                              health_samples=samples, graceful_restarts=restarts, retained_state=initial["state"])
                check("outage_health_and_ack_isolation", samples > 0)
                wait_for(settled, timeout=45)
                check("outage_durable_no_blind_retry", cli("event", delivery)["state"] == initial["state"])
                stop(receiver)
                check("stopped_receiver_reported", cli("status")["receiver_alive"] is False)
                cli("resolve", delivery, "--as", "discard", "--reason", "isolated unavailable-endpoint test complete")
                receiver = start()
                check("operator_resolution_survives_restart", cli("event", delivery)["state"] == "discarded")
                duplicate = http("/v1/events/fault", event)
                check("resolved_event_still_deduplicated", duplicate["duplicate"] and duplicate["delivery_id"] == delivery)
                stop(receiver)
                check("credentials_preserved_across_restarts", fingerprints == [hashlib.sha256(p.read_bytes()).hexdigest() for p in tokens])
                report["result"] = "PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
