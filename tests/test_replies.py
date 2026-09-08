import json
import tempfile
import unittest
import urllib.request
import urllib.error

from codex_monitor.errors import IngressError
from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.replies import ReplyStore


class RepliesTest(unittest.TestCase):
    def test_http_round_trip_is_source_scoped_durable_and_does_not_wake_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = Monitor(tmp, lambda _: self.fail("reply must never call Codex"))
            monitor.bind("work", "thread-user", "shared-local", ["build", "other"])
            parent = monitor.ingest("work", {"id": "build-7", "source": "build", "type": "build.failed", "data": {}})
            before = monitor.event(parent["delivery_id"])
            store = ReplyStore(tmp)
            reply = store.add(before, "decision-1", "Retry with the corrected config.")
            self.assertEqual(store.add(before, "decision-1", "Retry with the corrected config.")["reply_id"], reply["reply_id"])
            with self.assertRaises(IngressError):
                store.add(before, "decision-1", "Different content")
            server = Server(monitor, {"build": "build-secret", "other": "other-secret"}, "admin", port=0).start(dispatch=False)
            def request(path, token, post=False):
                req = urllib.request.Request(server.url + path, data=b"" if post else None,
                                             headers={"Authorization": "Bearer " + token})
                try:
                    with urllib.request.urlopen(req) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    with exc:
                        return exc.code, json.load(exc)
            try:
                self.assertEqual(request("/v1/replies", "admin")[0], 401)
                self.assertEqual(request("/v1/replies", "other-secret")[1]["data"], [])
                data = request("/v1/replies", "build-secret")[1]["data"]
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["event_id"], "build-7")
                self.assertEqual(request("/v1/replies/" + reply["reply_id"] + "/ack", "other-secret", True)[0], 404)
                self.assertEqual(request("/v1/replies/" + reply["reply_id"] + "/ack", "build-secret", True)[0], 200)
                self.assertEqual(request("/v1/replies/" + reply["reply_id"] + "/ack", "build-secret", True)[0], 200)
                self.assertEqual(request("/v1/replies", "build-secret")[1]["data"], [])
                self.assertEqual(ReplyStore(tmp).pending("build")["data"], [])
                self.assertTrue(ReplyStore(tmp).add(before, "decision-1", "Retry with the corrected config.")["duplicate"])
                self.assertEqual(monitor.event(parent["delivery_id"]), before)
            finally:
                server.close()

    def test_capacity_and_message_limits_preserve_retry_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplyStore(tmp, max_pending=1)
            parent = {"id": "delivery", "source": "source", "binding": "work", "event_id": "event"}
            store.add(parent, "one", "hello")
            self.assertTrue(store.add(parent, "one", "hello")["duplicate"])
            with self.assertRaises(IngressError) as caught:
                store.add(parent, "two", "world")
            self.assertEqual(caught.exception.status, 429)
            with self.assertRaises(IngressError):
                store.add(parent, "large", "x" * 16385)
            with self.assertRaises(IngressError):
                store.pending("source", 101)
