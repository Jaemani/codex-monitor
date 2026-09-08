from pathlib import Path
import tempfile
import unittest

from codex_monitor.errors import IngressError
from codex_monitor.monitor import Monitor


class RecordingSession:
    def __init__(self):
        self.calls = []

    def deliver(self, thread, client_id, text):
        self.calls.append((thread, client_id, text))
        return {"submission_id": "submission-1"}


class ManagedMonitorEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_endpoint_is_persisted_without_connecting_during_create_or_status(self):
        calls = []
        monitor = Monitor(self.root, lambda endpoint: calls.append(endpoint))
        endpoints = [
            "shared-local",
            "local",
            "unix://",
            "unix://" + str(self.root / "server.sock"),
            "ssh://build-host",
            "ws://127.0.0.1:8765",
            "ws://[::1]",
            "wss://app.example.com/codex",
        ]
        for index, endpoint in enumerate(endpoints):
            with self.subTest(endpoint=endpoint):
                created = monitor.managed_create(
                    "thread-offline", f"watch-{index}", str(self.root / f"file-{index}"),
                    endpoint=endpoint,
                )
                self.assertEqual(created["endpoint"], endpoint)
                binding = next(item for item in monitor.bindings() if item["name"] == created["binding"])
                self.assertEqual(binding["endpoint"], endpoint)
                self.assertEqual(monitor.managed_status("thread-offline", f"watch-{index}")["endpoint"], endpoint)
        self.assertEqual(calls, [])

    def test_invalid_or_unsafe_endpoints_are_rejected_before_persistence(self):
        monitor = Monitor(self.root, lambda _endpoint: self.fail("endpoint validation must not connect"))
        invalid = (
            "",
            "http://127.0.0.1:8765",
            "unix://relative.sock",
            "ssh://host;touch-file",
            "ssh://user@host",
            "ws://example.com:8765",
            "ws://user@127.0.0.1:8765",
            "wss://user:secret@example.com",
            "wss://example.com:bad",
            "ws://127.0.0.1:",
        )
        for index, endpoint in enumerate(invalid):
            with self.subTest(endpoint=endpoint), self.assertRaises(IngressError):
                monitor.managed_create(
                    "thread-invalid", f"watch-{index}", str(self.root / f"file-{index}"),
                    endpoint=endpoint,
                )
        self.assertEqual(monitor.managed_status("thread-invalid")["monitors"], [])

    def test_dispatch_uses_persisted_endpoint_and_exact_thread(self):
        session = RecordingSession()
        endpoints = []
        monitor = Monitor(self.root, lambda endpoint: endpoints.append(endpoint) or session)
        endpoint = "ws://127.0.0.1:8765"
        created = monitor.managed_create(
            "thread-target", "health", str(self.root / "health"), endpoint=endpoint,
        )
        receipt = monitor.ingest(created["binding"], {
            "id": "managed-health-change",
            "source": "managed/file",
            "type": "monitor.file.changed",
            "data": {"state": "changed"},
        })

        self.assertTrue(monitor.dispatch_once())
        self.assertEqual(endpoints, [endpoint])
        self.assertEqual(session.calls[0][0], "thread-target")
        self.assertIn(receipt["delivery_id"], session.calls[0][1])
        self.assertEqual(monitor.event(receipt["delivery_id"])["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
