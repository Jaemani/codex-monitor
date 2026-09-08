import tempfile
import unittest
from unittest.mock import patch

from codex_monitor.monitor import Monitor
from codex_monitor.requests import RequestStore
from codex_monitor.request_dispatch import RequestDispatcher


class RequestDispatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100.0
        self.monitor = Monitor(self.temp.name, lambda _: self.fail("unexpected model connection"),
                               clock=lambda: self.now)
        self.store = RequestStore(self.temp.name, clock=lambda: self.now)
        self.dispatcher = RequestDispatcher(self.monitor, self.store, batch_size=1)

    def create(self, name, **kwargs):
        self.monitor.bind(name, "thread-" + name, "shared-local", ["build"])
        receipt = self.monitor.ingest(name, {
            "id": "original-" + name, "source": "build", "type": "build.request", "data": {},
        })
        return self.store.create_for_parent("thread-" + name,
            self.monitor.event(receipt["delivery_id"]), name, {}, **kwargs)

    def advance(self, request, state):
        return self.store.transition(request["conversation_id"], request["source"],
            request["request_key"], update_id=state, target_state=state,
            expected_revision=request["revision"])

    def test_quiet_ack_and_explicit_completion_preserve_original_route(self):
        request = self.create("work")
        request = self.advance(request, "acknowledged")
        self.assertFalse(self.dispatcher.pump())
        self.assertEqual(self.monitor.status()["events"], {"pending": 1})
        request = self.advance(request, "completed")
        self.assertTrue(self.dispatcher.pump())
        recorded = self.store.get_by_id(request["request_id"])
        self.assertEqual(recorded["state"], "completed")
        notice = recorded["notifications"][0]
        event = self.monitor.event(notice["delivery_id"])
        self.assertEqual(event["state"], "pending")
        self.assertEqual(event["binding"], "work")
        self.assertEqual(event["source"], "build")
        self.assertEqual(event["envelope"]["data"]["conversation_id"], "thread-work")

    def test_crash_between_ingress_and_outbox_ack_replays_one_receipt(self):
        request = self.advance(self.create("crash"), "in_progress")
        with patch.object(self.store, "notification_accepted", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.dispatcher.pump()
        restarted = RequestStore(self.temp.name, clock=lambda: self.now)
        self.assertTrue(RequestDispatcher(self.monitor, restarted).pump())
        self.assertEqual(self.monitor.status()["events"], {"pending": 2})
        self.assertEqual(restarted.get_by_id(request["request_id"])["state"], "in_progress")
        self.assertEqual(restarted.pending_notifications(), [])

    def test_paused_batch_does_not_starve_other_conversation(self):
        paused = self.advance(self.create("paused"), "in_progress")
        self.monitor.enable("paused", False)
        self.now += .1
        ready = self.advance(self.create("ready"), "completed")
        self.assertFalse(self.dispatcher.pump())
        self.assertTrue(self.dispatcher.pump())
        self.assertEqual(self.store.get_by_id(ready["request_id"])["notifications"][0]["state"], "accepted")
        pending = self.store.get_by_id(paused["request_id"])["notifications"][0]
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual(pending["state"], "pending")

    def test_expiry_records_while_paused_and_notifies_after_resume(self):
        request = self.create("expiry", expires_in=1)
        self.monitor.enable("expiry", False)
        self.now += 2
        self.assertTrue(self.dispatcher.pump())
        expired = self.store.get_by_id(request["request_id"])
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["notifications"][0]["state"], "pending")
        self.monitor.enable("expiry", True)
        self.now += 2
        self.assertTrue(self.dispatcher.pump())
        self.assertEqual(self.store.get_by_id(request["request_id"])["notifications"][0]["state"], "accepted")

    def test_long_unicode_error_is_bounded_and_retried_after_recovery(self):
        request = self.advance(self.create("retry"), "completed")
        with patch.object(self.monitor, "ingest", side_effect=OSError("\u2603" * 2000)):
            self.assertFalse(self.dispatcher.pump())
        notice = self.store.get_by_id(request["request_id"])["notifications"][0]
        self.assertLessEqual(len(notice["error"].encode("utf-8")), 1024)
        self.assertEqual(notice["attempts"], 1)
        self.assertFalse(self.dispatcher.pump())
        self.now += 2
        self.assertTrue(self.dispatcher.pump())


if __name__ == "__main__":
    unittest.main()
