#!/usr/bin/env python3
"""Opt-in real model test on an owned App Server and synthetic conversation."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import socket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.cli import send
from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.session import Rpc, SessionPool


def wait_for(check, timeout=90):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result: return result
        time.sleep(.15)
    raise TimeoutError("canary condition not met")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true", required=True)
    p.add_argument("--report", default="/tmp/codex-monitor-real-canary.json")
    p.add_argument("--delivery-endpoint", choices=["shared-local"],
                   help="exercise the independent local queue writer used for Desktop and ordinary CLI")
    args = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="cm-", dir="/tmp") as tmp:
        root = Path(tmp)
        workdir = root / "work"
        workdir.mkdir()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"ws://127.0.0.1:{port}"
        delivery_endpoint = args.delivery_endpoint or endpoint
        process = subprocess.Popen(["codex", "app-server", "--listen", endpoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rpc, server = None, None
        pool = SessionPool()
        report = {"version": subprocess.check_output(["codex", "--version"], text=True).strip(),
                  "delivery_endpoint": delivery_endpoint, "client_ui_verified": False, "checks": {}}
        thread_id = None
        try:
            def listening():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1): return True
                except OSError: return False
            wait_for(listening, 10)
            rpc = Rpc(endpoint, timeout=15)
            thread = rpc.call("thread/start", {"cwd": str(workdir), "approvalPolicy": "never", "sandbox": "read-only",
                "developerInstructions": "This is an isolated codex-monitor test. Never read or change files or contact any service. For monitor events reply EVENT_ACK and its data.marker. For user messages reply USER_ACK and its marker. Do not use tools."})["thread"]
            thread_id = thread["id"]
            report["thread"] = thread_id
            rpc.call("thread/name/set", {"threadId": thread_id, "name": "codex-monitor isolated canary"})
            monitor = Monitor(root / "monitor", pool)
            monitor.bind("canary", thread_id, delivery_endpoint, ["webhook"])
            server = Server(monitor, {"webhook": "isolated-source-token"}, "isolated-admin-token", port=0).start()

            def turns():
                return rpc.call("thread/turns/list", {"threadId": thread_id, "itemsView": "full", "limit": 100})["data"]
            def items():
                return [item for turn in turns() for item in turn.get("items", [])]
            def seen(marker):
                return any(item.get("type") == "agentMessage" and marker in item.get("text", "") for item in items())
            rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "user marker USER_BEFORE"}]})
            wait_for(lambda: seen("USER_BEFORE"))
            baseline = len(turns())
            time.sleep(2)
            assert len(turns()) == baseline, "idle monitor started a turn"
            report["checks"]["idle_no_turn"] = True
            event = {"id": "real-webhook-1", "source": "webhook", "type": "webhook.random", "data": {"marker": "EVENT_FIRST"}}
            receipt = send(server.url, "canary", event, "isolated-source-token")
            duplicate = send(server.url, "canary", event, "isolated-source-token")
            assert receipt["delivery_id"] == duplicate["delivery_id"]
            wait_for(lambda: seen("EVENT_FIRST"))
            report["checks"]["event_wakes_same_thread"] = True
            if args.delivery_endpoint == "shared-local":
                assert pool(delivery_endpoint).rpc.call("thread/loaded/list", {})["data"] == []
                report["checks"]["writer_never_loaded_a_thread"] = True
            pool(delivery_endpoint).rpc.close()  # Lose only the monitor client; the human remains attached.
            # Submit user work and an event without waiting for the user turn to finish.
            rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "user marker USER_DURING"}]})
            second = send(server.url, "canary", {**event, "id": "real-webhook-2", "data": {"marker": "EVENT_DURING"}}, "isolated-source-token")
            wait_for(lambda: seen("EVENT_DURING"))
            report["checks"]["monitor_connection_recovered"] = True
            assert seen("USER_DURING"), "user work disappeared"
            rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "user marker USER_AFTER"}]})
            wait_for(lambda: seen("USER_AFTER"))
            report["checks"]["user_before_during_after"] = True
            first_client_id = monitor.event(receipt["delivery_id"])["client_id"]
            assert sum(item.get("clientId") == first_client_id for item in items()) == 1
            report["checks"]["duplicate_once"] = True
            assert pool(delivery_endpoint).reconcile(thread_id, first_client_id)
            report["checks"]["history_reconciliation"] = True
            assert list(workdir.iterdir()) == [], "model modified the isolated workspace"
            report["checks"]["source_checkout_unchanged"] = True
            report["result"] = "PASS"
        except Exception as exc:
            report.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if server: server.close()
            pool.close()
            if rpc:
                if thread_id:
                    try: rpc.call("thread/archive", {"threadId": thread_id})
                    except Exception: pass
                rpc.close()
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
