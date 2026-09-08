import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from codex_monitor import cli
from codex_monitor.monitor import Monitor
from codex_monitor.requests import RequestStore


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class RequestCapabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.json").write_text(json.dumps({
            "version": 1, "port": 8766, "sources": {}, "limits": {},
        }))
        (self.root / "admin.token").write_text("admin-secret\n")
        monitor = Monitor(self.root, lambda _endpoint: mock.Mock())
        monitor.bind("work", "thread-user", "shared-local", ["build"])
        self.receipt = monitor.ingest("work", {
            "id": "build-request", "source": "build", "type": "build.failed",
            "data": {"job": 17},
        })
        self.pool = mock.Mock()

    def run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(cli, "SessionPool", return_value=self.pool), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--state", str(self.root), *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def old_receiver(self):
        opener = mock.Mock()
        opener.open.return_value = _Response({"capabilities": {"request_lifecycle": False}})
        return opener

    def test_track_fails_closed_before_mutating_when_active_receiver_is_old(self):
        opener = self.old_receiver()
        with mock.patch.object(cli, "process_alive", return_value=True), \
                mock.patch.object(cli.urllib.request, "build_opener", return_value=opener):
            code, _stdout, stderr = self.run_cli(
                "request", "track", self.receipt["delivery_id"],
                "--key", "build-17", "--thread", "thread-user",
            )
        self.assertEqual(code, 2)
        self.assertIn("does not support request_lifecycle", stderr)
        self.assertEqual(RequestStore(self.root).list_requests("thread-user")["data"], [])
        opener.open.assert_called_once()

    def test_update_fails_closed_before_mutating_when_active_receiver_is_old(self):
        with mock.patch.object(cli, "process_alive", return_value=False):
            code, _stdout, stderr = self.run_cli(
                "request", "track", self.receipt["delivery_id"],
                "--key", "build-17", "--thread", "thread-user",
            )
        self.assertEqual(code, 0, stderr)
        request = RequestStore(self.root).get("thread-user", "build", "build-17")
        self.assertEqual(request["state"], "received")

        opener = self.old_receiver()
        with mock.patch.object(cli, "process_alive", return_value=True), \
                mock.patch.object(cli.urllib.request, "build_opener", return_value=opener):
            code, _stdout, stderr = self.run_cli(
                "request", "update", request["request_id"], "--thread", "thread-user",
                "--state", "in_progress", "--update-id", "update-1", "--revision", "0",
                "--message", "started",
            )
        self.assertEqual(code, 2)
        self.assertIn("does not support request_lifecycle", stderr)
        unchanged = RequestStore(self.root).get("thread-user", "build", "build-17")
        self.assertEqual(unchanged["state"], "received")
        self.assertEqual(unchanged["revision"], 0)
        self.assertEqual(unchanged["notifications"], [])
        opener.open.assert_called_once()

    def test_request_tracking_remains_offline_when_no_receiver_is_running(self):
        opener = mock.Mock()
        with mock.patch.object(cli, "process_alive", return_value=False), \
                mock.patch.object(cli.urllib.request, "build_opener", return_value=opener):
            code, _stdout, stderr = self.run_cli(
                "request", "track", self.receipt["delivery_id"],
                "--key", "build-17", "--thread", "thread-user",
            )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(opener.open.called)
        self.assertEqual(
            RequestStore(self.root).get("thread-user", "build", "build-17")["state"],
            "received",
        )


if __name__ == "__main__":
    unittest.main()
