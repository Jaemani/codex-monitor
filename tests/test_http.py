import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
import random
import socket
import threading
import time
from urllib.parse import urlsplit

from codex_monitor.monitor import Monitor
from codex_monitor.http import Server
from test_monitor import RecordingSession


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = RecordingSession()
        self.monitor = Monitor(Path(self.temp.name), lambda _: self.session)
        self.monitor.bind("work", "thread-user", "local", ["build"])
        self.server = Server(self.monitor, {"build": "test-source-secret"}, "test-admin-secret", port=0)
        self.server.start(dispatch=False)
        self.addCleanup(self.server.close)

    def request(self, data=None, token="test-source-secret", path="/v1/events/work", headers=None):
        request = urllib.request.Request(self.server.url + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_authenticated_concurrent_duplicates_have_one_receipt(self):
        event = {"id": "same", "source": "build", "type": "build.failed", "data": {"job": 1}}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.request(event), range(24)))
        self.assertEqual({code for code, _ in responses}, {202})
        self.assertEqual(len({r["delivery_id"] for _, r in responses}), 1)
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)

    def test_auth_source_binding_origin_and_body_limits(self):
        event = {"id": "one", "source": "build", "type": "notice", "data": {}}
        self.assertEqual(self.request(event, token="bad")[0], 401)
        self.assertEqual(self.request({**event, "source": "other"})[0], 403)
        self.assertEqual(self.request(event, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request({**event, "data": "x" * 40000})[0], 413)
        self.assertEqual(self.request(None, path="/v1/status")[0], 401)
        code, status = self.request(None, token="test-admin-secret", path="/v1/status")
        self.assertEqual(code, 200)
        self.assertEqual(status["events"], {})
        self.assertTrue(status["capabilities"]["managed_json_predicates"])
        self.assertTrue(status["capabilities"]["request_lifecycle"])

    def test_seeded_random_webhook_burst_crosses_real_http_boundary(self):
        ids = list(range(30)) * 2
        random.Random(314159).shuffle(ids)
        def post(i):
            return self.request({"id": str(i), "source": "build", "type": "webhook.random", "data": {"marker": i}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(post, ids))
        self.assertEqual({code for code, _ in responses}, {202})
        self.assertEqual(len({r["delivery_id"] for _, r in responses}), 30)
        while self.monitor.dispatch_once(): pass
        self.assertEqual(len(self.session.calls), 30)

    def test_stalled_connection_bounds_admission_and_recovers_without_event_loss(self):
        server = Server(self.monitor, {"build": "test-source-secret"}, "test-admin-secret",
                        port=0, max_connections=1).start(dispatch=False)
        self.addCleanup(server.close)
        previous = self.server
        self.server = server
        self.addCleanup(setattr, self, "server", previous)
        endpoint = urlsplit(server.url)
        stalled = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
        self.addCleanup(stalled.close)
        stalled.sendall(b"POST /v1/events/work HTTP/1.0\r\n")
        event = {"id": "after-overload", "source": "build", "type": "notice", "data": {}}
        code, _ = self.request(event)
        self.assertEqual(code, 503)
        self.assertFalse(self.monitor.dispatch_once())
        stalled.close()
        deadline = time.monotonic() + 2
        while True:
            code, receipt = self.request(event)
            if code != 503 or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        self.assertEqual(code, 202)
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)

    def test_slow_drip_rejection_releases_accept_loop_promptly(self):
        server = Server(self.monitor, {"build": "test-source-secret"}, "test-admin-secret",
                        port=0, max_connections=1).start(dispatch=False)
        self.addCleanup(server.close)
        previous = self.server
        self.server = server
        self.addCleanup(setattr, self, "server", previous)
        endpoint = urlsplit(server.url)
        occupied = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
        slow = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
        self.addCleanup(occupied.close)
        self.addCleanup(slow.close)
        occupied.sendall(b"POST /v1/events/work HTTP/1.0\r\n")
        slow.sendall(b"POST /v1/events/work HTTP/1.0\r\nX-Slow: ")
        dripping = True

        def drip():
            while dripping:
                try:
                    slow.sendall(b"x")
                except OSError:
                    return
                time.sleep(.05)

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            code, _ = self.request({"id": "slow-drip", "source": "build", "type": "notice", "data": {}})
        finally:
            dripping = False
            thread.join(timeout=1)
            slow.close()
            occupied.close()
        self.assertEqual(code, 503)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.monitor.dispatch_once())


if __name__ == "__main__":
    unittest.main()
