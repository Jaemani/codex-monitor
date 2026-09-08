#!/usr/bin/env python3
"""One-shot lifecycle test supervisor; run under an owned launchd job.

Uses its wheel-installed interpreter. Writes evidence before one optional result
event. No periodic events, model polling, or automatic repetition.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--thread")
    parser.add_argument("--seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.seconds < 30:
        parser.error("--seconds must be at least 30")
    if args.report.exists():
        parser.error("report already exists; never repeat this run implicitly")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    child_report = args.report.with_suffix(".child.json")
    state = args.report.parent / ".runtime" / args.report.stem
    report = {"result": "RUNNING", "supervisor": "one-shot lifecycle-runner.py",
              "started_at": datetime.now(timezone.utc).isoformat(),
              "outage_seconds_requested": args.seconds, "python_executable": sys.executable}
    save(args.report, report)
    try:
        command = [sys.executable, str(Path(__file__).with_name("lifecycle-canary.py")),
                   "--run", "--outage-seconds", str(args.seconds), "--report", str(child_report)]
        process = subprocess.Popen(command, cwd="/tmp", start_new_session=True)
        try:
            returncode = process.wait(timeout=args.seconds + 300)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if not child_report.exists():
            raise RuntimeError(f"canary exited {returncode} without a report")
        report.update(json.loads(child_report.read_text()))
        if returncode:
            report["result"] = "FAIL"
    except Exception as exc:
        report.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.report, report)
    if args.thread:
        from codex_monitor.monitor import Monitor
        from codex_monitor.session import SessionPool
        pool = SessionPool()
        try:
            monitor = Monitor(state, pool)
            monitor.bind("completion", args.thread, "shared-local", ["test"])
            receipt = monitor.ingest("completion", {
                "id": args.report.stem, "source": "test", "type": "monitor.lifecycle.completed",
                "data": {"result": report["result"], "report": str(args.report),
                         "outage_seconds": report.get("outage_seconds_observed")}})
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                event = monitor.event(receipt["delivery_id"])
                if event["state"] in ("accepted", "dead", "uncertain"):
                    break
                monitor.dispatch_once()
                time.sleep(.1)
            report["notification"] = {k: monitor.event(receipt["delivery_id"])[k]
                                      for k in ("state", "client_id", "id")}
        except Exception as exc:
            report["notification"] = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            pool.close()
        save(args.report, report)
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
