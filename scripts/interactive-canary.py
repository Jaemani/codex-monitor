#!/usr/bin/env python3
"""Opt-in handshake with a human using the actual CLI/Desktop test conversation."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.monitor import Monitor
from codex_monitor.session import Rpc, SessionPool


def main():
    parser = argparse.ArgumentParser(description="Use only a dedicated disposable test conversation.")
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--surface", choices=["cli", "desktop"], required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    pool = SessionPool()
    rpc = Rpc(args.endpoint)
    tag = uuid.uuid4().hex[:8]
    ready, after, event = f"MONITOR_READY_{tag}", f"MONITOR_AFTER_{tag}", f"MONITOR_EVENT_{tag}"
    report = {"surface": args.surface, "thread": args.thread, "endpoint": args.endpoint, "result": "FAIL"}
    def items():
        turns = rpc.call("thread/turns/list", {"threadId": args.thread, "itemsView": "full", "limit": 100})["data"]
        return [item for turn in turns for item in turn.get("items", [])]
    def human(marker):
        return any(item.get("type") == "userMessage" and not str(item.get("clientId", "")).startswith("codex-monitor:")
                   and marker in json.dumps(item.get("content", [])) for item in items())
    def await_condition(test):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if test(): return
            time.sleep(.5)
        raise TimeoutError("waiting for human UI verification timed out")
    try:
        pool(args.endpoint).check_target(args.thread)
        print(f"In the dedicated {args.surface} conversation, type: {ready}", flush=True)
        await_condition(lambda: human(ready))
        with tempfile.TemporaryDirectory(prefix="codex-monitor-interactive-") as tmp:
            monitor = Monitor(tmp, pool)
            monitor.bind("ui", args.thread, args.endpoint, ["test"])
            receipt = monitor.ingest("ui", {"id": tag, "source": "test", "type": "test.event", "data": {
                "marker": event, "instruction": "For this authorized test, reply with this marker. Do not use tools."}})
            monitor.dispatch_once()
            state = monitor.event(receipt["delivery_id"])["state"]
            if state != "accepted": raise RuntimeError(f"event not accepted: {state}")
            print(f"Wait for {event} to appear in the UI, then type: {after}", flush=True)
            await_condition(lambda: human(after))
            if not any(item.get("type") == "agentMessage" and event in item.get("text", "") for item in items()):
                raise RuntimeError("event response marker not found")
            report.update(result="PASS", user_before_and_after=True, event_response_seen=True,
                          ui_verification="human followed the visible-marker instruction")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        pool.close(); rpc.close()
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
