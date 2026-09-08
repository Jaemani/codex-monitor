#!/usr/bin/env python3
"""Seeded event/retry/restart stress at the public Monitor interface. No models."""
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.monitor import Monitor
from codex_monitor.errors import Retryable, Uncertain


def run(seed, count):
    rng = random.Random(seed)
    accepted = []
    failures = set()
    now = [100.0]
    class Session:
        def deliver(self, thread, client_id, text):
            if client_id not in failures:
                failures.add(client_id)
                sample = rng.random()
                if sample < .15: raise Retryable("injected pre-send disconnect")
                if sample < .30:
                    accepted.append(client_id)
                    raise Uncertain("injected lost response after acceptance")
            if client_id in accepted: raise AssertionError("duplicate external effect")
            accepted.append(client_id)
            return {"submission_id": client_id}
        def reconcile(self, thread, client_id):
            return {"submission_id": client_id} if client_id in accepted else None
    session = Session()
    with tempfile.TemporaryDirectory(prefix="cm-stress-") as tmp:
        create = lambda: Monitor(tmp, lambda _: session, clock=lambda: now[0], rate_limit=count*5, max_pending=count*2, max_age=100000)
        monitor = create()
        monitor.bind("stress", "thread-user", "local", ["random"])
        inputs = list(range(count)) * 3
        rng.shuffle(inputs)
        receipts = set()
        for i in inputs:
            receipts.add(monitor.ingest("stress", {"id": str(i), "source": "random", "type": "random.webhook", "data": i})["delivery_id"])
            if rng.random() < .1: monitor = create()
        for _ in range(count*10):
            monitor.dispatch_once()
            now[0] += 31
            if rng.random() < .1: monitor = create()
            if len(accepted) == count and monitor.status()["events"] == {"accepted": count}: break
        assert len(receipts) == count
        assert len(accepted) == len(set(accepted)) == count
        assert monitor.status()["events"] == {"accepted": count}, monitor.status()
    return {"seed": seed, "unique_events": count, "ingress_attempts": count*3, "result": "PASS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--report", default="/tmp/codex-monitor-stress.json")
    args = parser.parse_args()
    results = [run(seed, args.events) for seed in range(args.seeds)]
    report = {"result": "PASS", "seeds": args.seeds, "unique_events": args.events*args.seeds,
              "ingress_attempts": args.events*args.seeds*3, "runs": results}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k:v for k,v in report.items() if k != "runs"}))
