import tempfile
import unittest
from pathlib import Path

from codex_monitor.monitor import Monitor


class RecordingSession:
    def __init__(self):
        self.calls = []

    def deliver(self, thread, client_id, text):
        self.calls.append((thread, client_id, text))
        return {"submission_id": "queue-1"}

    def reconcile(self, thread, client_id):
        return {"submission_id": "queue-1"} if any(x[1] == client_id for x in self.calls) else None


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = RecordingSession()
        self.monitor = Monitor(Path(self.temp.name), lambda _: self.session)
        self.monitor.bind("work", "thread-user", "local", ["build"])

    def test_idle_is_silent_and_webhook_reaches_exact_user_conversation_once(self):
        self.monitor.dispatch_once()
        self.assertEqual(self.session.calls, [])
        event = {"id": "build-1", "source": "build", "type": "build.failed", "data": {"job": 17}}
        receipt = self.monitor.ingest("work", event)
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.session.calls[0][0], "thread-user")
        self.assertIn('job: 17', self.session.calls[0][2])
        self.assertEqual(self.monitor.event(receipt["delivery_id"])["state"], "accepted")
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)

    def test_duplicate_webhooks_are_one_delivery_but_conflicting_id_is_rejected(self):
        from codex_monitor.errors import IngressError
        envelope = {"id": "same", "source": "build", "type": "build.failed", "data": {"job": 17}}
        first = self.monitor.ingest("work", envelope)
        second = self.monitor.ingest("work", envelope)
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertTrue(second["duplicate"])
        with self.assertRaises(IngressError):
            self.monitor.ingest("work", {**envelope, "data": {"job": 18}})
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)

    def test_untrusted_source_and_malformed_input_cannot_wake_conversation(self):
        from codex_monitor.errors import IngressError
        for envelope in (None, {}, {"id": "x", "source": "outsider", "type": "notice", "data": {}},
                         {"id": "x", "source": "build", "type": "notice", "data": {}, "hops": 9}):
            with self.subTest(envelope=envelope), self.assertRaises(IngressError):
                self.monitor.ingest("work", envelope)
        self.monitor.dispatch_once()
        self.assertEqual(self.session.calls, [])

    def test_retry_is_bounded_and_preserves_fifo_without_blocking_another_binding(self):
        from codex_monitor.errors import Retryable
        now = [100.0]
        monitor = Monitor(Path(self.temp.name), lambda _: self.session, clock=lambda: now[0], max_attempts=2)
        monitor.bind("other", "thread-other", "local", ["build"])
        first = monitor.ingest("work", {"id": "1", "source": "build", "type": "notice", "data": 1})
        monitor.ingest("work", {"id": "2", "source": "build", "type": "notice", "data": 2})
        other = monitor.ingest("other", {"id": "3", "source": "build", "type": "notice", "data": 3})
        original = self.session.deliver
        def offline(thread, client_id, text):
            if thread == "thread-user":
                raise Retryable("offline")
            return original(thread, client_id, text)
        self.session.deliver = offline
        monitor.dispatch_once()
        monitor.dispatch_once()
        self.assertEqual(monitor.event(other["delivery_id"])["state"], "accepted")
        self.assertEqual(monitor.event(first["delivery_id"])["attempts"], 1)
        now[0] += 100
        monitor.dispatch_once()
        self.assertEqual(monitor.event(first["delivery_id"])["state"], "dead")

    def test_crash_after_queue_acceptance_recovers_without_duplicate_delivery(self):
        class Crash(BaseException):
            pass
        original = self.session.deliver
        def accepted_then_crash(*args):
            original(*args)
            raise Crash()
        self.session.deliver = accepted_then_crash
        receipt = self.monitor.ingest("work", {"id": "crash", "source": "build", "type": "notice", "data": {}})
        with self.assertRaises(Crash):
            self.monitor.dispatch_once()
        restarted = Monitor(Path(self.temp.name), lambda _: self.session)
        restarted.dispatch_once()
        self.assertEqual(restarted.event(receipt["delivery_id"])["state"], "accepted")
        self.assertEqual(len(self.session.calls), 1)

    def test_unknown_acceptance_requires_operator_decision_not_blind_retry(self):
        from codex_monitor.errors import Uncertain
        def lost(*args):
            raise Uncertain("response lost")
        self.session.deliver = lost
        receipt = self.monitor.ingest("work", {"id": "unknown", "source": "build", "type": "notice", "data": {}})
        self.monitor.dispatch_once()
        self.monitor.dispatch_once()
        self.assertEqual(self.monitor.event(receipt["delivery_id"])["state"], "uncertain")
        self.monitor.resolve(receipt["delivery_id"], "discard", "user checked conversation")
        self.assertEqual(self.monitor.event(receipt["delivery_id"])["state"], "discarded")

    def test_expired_event_does_not_wake_and_invalid_numeric_data_is_rejected(self):
        from codex_monitor.errors import IngressError
        now = [100.0]
        monitor = Monitor(Path(self.temp.name), lambda _: self.session, clock=lambda: now[0], max_age=10)
        receipt = monitor.ingest("work", {"id": "old", "source": "build", "type": "notice", "data": {}})
        now[0] = 111
        monitor.dispatch_once()
        self.assertEqual(monitor.event(receipt["delivery_id"])["state"], "dead")
        self.assertEqual(self.session.calls, [])
        with self.assertRaises(IngressError):
            monitor.ingest("work", {"id": "nan", "source": "build", "type": "notice", "data": float("nan")})


if __name__ == "__main__":
    unittest.main()
