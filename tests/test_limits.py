import concurrent.futures
import json
from pathlib import Path
import random
import tempfile
import unittest

from codex_monitor.monitor import Monitor
from codex_monitor.errors import IngressError
from test_monitor import RecordingSession


class LimitsTest(unittest.TestCase):
    def test_seeded_random_webhooks_deduplicate_across_restarts(self):
        for seed in (7, 29, 101):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as tmp:
                session = RecordingSession()
                monitor = Monitor(Path(tmp), lambda _: session)
                monitor.bind("work", "thread-user", "local", ["random"])
                ids = list(range(80)) * 3
                random.Random(seed).shuffle(ids)
                receipts = set()
                for i, event_id in enumerate(ids):
                    receipts.add(monitor.ingest("work", {"id": str(event_id), "source": "random", "type": "webhook", "data": event_id})["delivery_id"])
                    if i % 11 == 0:
                        monitor = Monitor(Path(tmp), lambda _: session)
                while monitor.dispatch_once():
                    pass
                self.assertEqual(len(receipts), 80)
                self.assertEqual(len(session.calls), 80)
                delivered = [monitor.event(client_id.removeprefix("codex-monitor:"))["envelope"]["data"]
                             for _, client_id, _ in session.calls]
                self.assertEqual(delivered, list(dict.fromkeys(ids)))

    def test_agent_dialogue_limits_and_ack_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = RecordingSession()
            monitor = Monitor(Path(tmp), lambda _: session, trace_limit=4)
            monitor.bind("a", "thread-a", "local", ["agent-b"])
            monitor.bind("b", "thread-b", "local", ["agent-a"])
            for hop in range(4):
                target, source = ("b", "agent-a") if hop % 2 == 0 else ("a", "agent-b")
                monitor.ingest(target, {"id": str(hop), "source": source, "type": "agent.message", "data": "reply", "trace_id": "dialogue-1", "hops": hop})
                monitor.dispatch_once()
            with self.assertRaises(IngressError):
                monitor.ingest("b", {"id": "loop", "source": "agent-a", "type": "agent.message", "data": "reply", "trace_id": "dialogue-1", "hops": 4})
            receipt = monitor.ingest("b", {"id": "ack", "source": "agent-a", "type": "agent.ack", "data": "recorded"})
            monitor.dispatch_once()
            self.assertEqual(monitor.event(receipt["delivery_id"])["state"], "ignored")
            self.assertEqual(len(session.calls), 4)

    def test_queue_backpressure_rate_limits_and_disabled_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = Monitor(Path(tmp), lambda _: RecordingSession(), max_pending=2, rate_limit=3)
            monitor.bind("work", "thread-user", "local", ["build"])
            event = lambda i: {"id": str(i), "source": "build", "type": "notice", "data": i}
            monitor.ingest("work", event(1)); monitor.ingest("work", event(2))
            with self.assertRaises(IngressError): monitor.ingest("work", event(3))
            monitor.dispatch_once(); monitor.ingest("work", event(3)); monitor.dispatch_once()
            with self.assertRaises(IngressError): monitor.ingest("work", event(4))
            monitor.enable("work", False)
            self.assertFalse(monitor.dispatch_once())
            with self.assertRaises(IngressError): monitor.ingest("work", event(5))
