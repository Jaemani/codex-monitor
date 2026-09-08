#!/usr/bin/env python3
"""Opt-in event to an explicitly selected Desktop conversation, then verify history.

Run --enqueue only in a conversation authorized for a visible test message.
After Desktop handles the event, run --check with the same report path.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.cli import send
from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.session import Rpc, SessionPool


def assess_history(turns, client_id, marker):
    # App Server returns newest turns first; items within each turn are ordered.
    items = [item for turn in reversed(turns) for item in turn.get("items", [])]
    positions = [index for index, item in enumerate(items)
                 if item.get("type") == "userMessage" and item.get("clientId") == client_id]
    human = lambda item: item.get("type") == "userMessage" and not str(item.get("clientId", "")).startswith("codex-monitor:")
    position = positions[0] if len(positions) == 1 else None
    return {
        "event_history_count": len(positions),
        "event_response_recorded": position is not None and any(
            item.get("type") == "agentMessage" and marker in item.get("text", "")
            for item in items[position + 1:]),
        "user_input_before_event": position is not None and any(map(human, items[:position])),
        "user_input_after_event": position is not None and any(map(human, items[position + 1:])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enqueue", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--thread")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=int, default=0,
                        help="one-shot delay before enqueue for an explicitly prepared app restart test")
    args = parser.parse_args()
    if args.enqueue and not args.thread:
        parser.error("--enqueue requires the explicitly authorized --thread")
    if not 0 <= args.delay_seconds <= 300 or (args.delay_seconds and not args.enqueue):
        parser.error("--delay-seconds must be 0..300 and is only valid with --enqueue")
    if args.enqueue and args.report.exists():
        parser.error("report already exists; use --check, do not send a second event")
    rpc = Rpc("shared-local")
    pool = SessionPool()
    server = None
    report = {"result": "FAIL", "surface": "desktop", "endpoint": "shared-local",
              "client_ui_verified": False}
    try:
        if args.enqueue:
            marker = "MONITOR_DESKTOP_EVENT_" + uuid.uuid4().hex[:12]
            report.update(thread=args.thread, marker=marker)
            if args.delay_seconds:
                time.sleep(args.delay_seconds)
            report["enqueue_started_at"] = datetime.now(timezone.utc).isoformat()
            pool("shared-local").check_target(args.thread)
            with tempfile.TemporaryDirectory(prefix="cm-desktop-test-") as tmp:
                monitor = Monitor(tmp, pool)
                monitor.bind("desktop", args.thread, "shared-local", ["test"])
                server = Server(monitor, {"test": "isolated-test-source"}, "isolated-test-admin", port=0).start()
                envelope = {"id": marker, "source": "test", "type": "monitor.canary", "data": {
                    "marker": marker,
                    "test_case": "desktop-delivery"}}
                receipt = send(server.url, "desktop", envelope, "isolated-test-source")
                duplicate = send(server.url, "desktop", envelope, "isolated-test-source")
                if receipt["delivery_id"] != duplicate["delivery_id"]:
                    raise AssertionError("duplicate receipt mismatch")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    value = monitor.event(receipt["delivery_id"])
                    if value["state"] == "accepted":
                        break
                    if value["state"] in ("dead", "uncertain"):
                        raise RuntimeError(value["error"])
                    time.sleep(.1)
                else:
                    raise TimeoutError("queue acceptance timed out")
                report.update(result="AWAITING_DESKTOP_RESPONSE", client_id=value["client_id"],
                              submission_id=value["submission_id"], duplicate_receipt_preserved=True)
                if pool("shared-local").rpc.call("thread/loaded/list", {})["data"]:
                    raise AssertionError("queue writer unexpectedly loaded a thread")
                report["writer_loaded_threads"] = []
                server.close()
                server = None
        else:
            report = json.loads(args.report.read_text())
            turns = rpc.call("thread/turns/list", {"threadId": report["thread"], "itemsView": "full", "limit": 100})["data"]
            assessment = assess_history(turns, report["client_id"], report["marker"])
            report.update(assessment)
            report["result"] = (
                "DESKTOP_RESPONSE_RECORDED" if assessment["event_history_count"] == 1 and assessment["event_response_recorded"]
                else "DESKTOP_EVENT_CONSUMED" if assessment["event_history_count"] == 1
                else "AWAITING_DESKTOP_RESPONSE")
            # A transcript response proves owner consumption, not pixel visibility.
            report["client_ui_verified"] = False
    except Exception as exc:
        report.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
    finally:
        if server:
            server.close()
        pool.close()
        rpc.close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return 2 if report["result"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
