import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import codex_monitor.managed as managed_module
from codex_monitor.cli import main
from codex_monitor.errors import IngressError
from codex_monitor.http import Server
from codex_monitor.managed import ManagedSupervisor
from codex_monitor.lock import ProcessLock
from codex_monitor.monitor import Monitor
from codex_monitor.replies import ReplyStore


class RecordingSession:
    def __init__(self):
        self.calls = []

    def deliver(self, thread, client_id, text):
        self.calls.append((thread, client_id, text))
        return {"submission_id": "queue-" + str(len(self.calls))}

    def reconcile(self, thread, client_id):
        return {"submission_id": "queue-1"} if any(call[1] == client_id for call in self.calls) else None


class ManagedMonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = RecordingSession()
        self.monitor = Monitor(Path(self.temp.name), lambda _: self.session)

    def _supervisor(self):
        now = [0.0]
        supervisor = ManagedSupervisor(self.monitor, poll_interval=.01, clock=lambda: now[0])
        return supervisor, now

    def test_same_name_is_independent_per_thread_and_management_does_not_call_codex(self):
        first = Path(self.temp.name) / "first"
        second = Path(self.temp.name) / "second"
        first.write_text("one")
        second.write_text("two")
        left = self.monitor.managed_create("thread-a", "health", str(first))
        right = self.monitor.managed_create("thread-b", "health", str(second))
        self.assertNotEqual(left["id"], right["id"])
        self.assertNotEqual(left["binding"], right["binding"])
        self.assertEqual(self.monitor.managed_status("thread-a")["monitors"][0]["name"], "health")
        with self.assertRaisesRegex(Exception, "already exists"):
            self.monitor.managed_create("thread-a", "health", str(first))
        self.assertEqual(self.session.calls, [])

    def test_change_only_pause_resume_remove_and_restart(self):
        path = Path(self.temp.name) / "state"
        path.write_text("one")
        created = self.monitor.managed_create("thread-a", "state", str(path), .1)
        supervisor, now = self._supervisor()
        supervisor.poll_once()  # establish the silent baseline
        path.write_text("two")
        now[0] = .1
        supervisor.poll_once()
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)
        now[0] = .2
        supervisor.poll_once()  # unchanged content is silent
        self.assertEqual(len(self.session.calls), 1)

        self.monitor.managed_set_enabled("thread-a", "state", False)
        path.write_text("three")
        now[0] = .3
        supervisor.poll_once()
        self.assertEqual(len(self.session.calls), 1)
        self.monitor.managed_set_enabled("thread-a", "state", True)
        now[0] = .4
        supervisor.poll_once()  # resume reports the change since the old baseline
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 2)

        old_pending = self.monitor.ingest(
            created["binding"],
            {"id": "old-pending", "source": "managed/file", "type": "monitor.file.changed", "data": {"old": True}},
        )
        removed = self.monitor.managed_remove("thread-a", "state")
        self.assertTrue(removed["removed"])
        path.write_text("four")
        now[0] = .5
        supervisor.poll_once()
        self.assertEqual(len(self.session.calls), 2)
        recreated = self.monitor.managed_create("thread-a", "state", str(path), .1)
        self.assertNotEqual(created["id"], recreated["id"])
        self.assertNotEqual(created["binding"], recreated["binding"])
        old_binding = next(item for item in self.monitor.bindings() if item["name"] == created["binding"])
        self.assertEqual(old_binding["enabled"], 0)
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(self.monitor.event(old_pending["delivery_id"])["state"], "pending")

        # A new supervisor can dispatch an accepted durable event exactly once.
        supervisor.close()
        restarted = ManagedSupervisor(self.monitor, poll_interval=.01, clock=lambda: .6)
        restarted.poll_once()
        self.assertEqual(len(self.session.calls), 2)

    def test_fifo_is_rejected_without_blocking_and_recovery_is_one_change(self):
        path = Path(self.temp.name) / "input"
        os.mkfifo(path)
        self.monitor.managed_create("thread-a", "input", str(path), .1)
        supervisor, now = self._supervisor()
        supervisor.poll_once()
        status = self.monitor.managed_status("thread-a", "input")
        self.assertIn("not_regular_file", status["last_sample_error"])
        self.assertEqual(self.session.calls, [])
        path.unlink()
        path.write_text("ready")
        now[0] = .1
        supervisor.poll_once()
        self.monitor.dispatch_once()
        self.assertEqual(len(self.session.calls), 1)
        now[0] = .2
        supervisor.poll_once()
        self.assertEqual(len(self.session.calls), 1)

    def test_pause_race_cannot_resurrect_running_worker(self):
        path = Path(self.temp.name) / "race"
        path.write_text("one")
        self.monitor.managed_create("thread-a", "race", str(path), .1)
        supervisor, _ = self._supervisor()
        original = managed_module.safe_file_sample

        def pause_during_sample(value):
            self.monitor.managed_set_enabled("thread-a", "race", False)
            return original(value)

        with patch.object(managed_module, "safe_file_sample", side_effect=pause_during_sample):
            supervisor.poll_once()
        status = self.monitor.managed_status("thread-a", "race")
        self.assertFalse(status["enabled"])
        self.assertEqual(status["collector_status"], "stopped")

    def test_status_is_stale_without_receiver_lock_and_bad_checkpoint_is_reported(self):
        path = Path(self.temp.name) / "status"
        path.write_text("one")
        created = self.monitor.managed_create("thread-a", "status", str(path), .1)
        supervisor, _ = self._supervisor()
        with ProcessLock(Path(self.temp.name) / "serve.lock"):
            supervisor.poll_once()
            self.assertEqual(self.monitor.managed_status("thread-a", "status")["collector_status"], "running")
        self.assertEqual(self.monitor.managed_status("thread-a", "status")["collector_status"], "stale")
        checkpoint = self.monitor._managed_checkpoint(self.monitor.root, created["id"])
        checkpoint.write_text("[]")
        malformed = self.monitor.managed_status("thread-a", "status")
        self.assertIn("checkpoint must contain", malformed["checkpoint_error"])
        self.assertIsNone(malformed["last_sample"])

    def test_interval_and_resource_limits_are_finite(self):
        path = Path(self.temp.name) / "limits"
        path.write_text("one")
        for interval in (0, .09, float("nan"), float("inf"), 86400.1):
            with self.subTest(interval=interval), self.assertRaises(IngressError):
                self.monitor.managed_create("thread-a", "watch-" + str(interval).replace(".", "_"), str(path), interval)

    def test_cli_context_is_required_or_exact_environment_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with patch("codex_monitor.cli.SessionPool", return_value=Mock()):
                self.assertEqual(main(["--state", str(state), "init"]), 0)
                errors = io.StringIO()
                with redirect_stderr(errors), patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CODEX_THREAD_ID", None)
                    self.assertEqual(main(["--state", str(state), "monitor", "list"]), 2)
                self.assertIn("--thread or CODEX_THREAD_ID", errors.getvalue())
                output = io.StringIO()
                with redirect_stdout(output), patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env"}):
                    self.assertEqual(main(["--state", str(state), "monitor", "list"]), 0)
                self.assertEqual(json.loads(output.getvalue())["thread"], "thread-env")
                errors = io.StringIO()
                with redirect_stderr(errors), patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env"}):
                    self.assertEqual(main(["--state", str(state), "monitor", "list", "--thread", "other"]), 2)
                self.assertIn("does not match", errors.getvalue())

    def test_managed_only_server_accepts_empty_external_source_registry(self):
        server = Server(self.monitor, {}, "admin-token", port=0).start(dispatch=False)
        try:
            self.assertTrue(server.url.startswith("http://127.0.0.1:"))
        finally:
            server.close()

    def test_managed_event_reply_is_rejected_without_outbox_insertion(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with patch("codex_monitor.cli.SessionPool", return_value=Mock()):
                self.assertEqual(main(["--state", str(state), "init"]), 0)
            monitor = Monitor(state, lambda _: self.session)
            file_path = Path(temporary) / "event-source"
            file_path.write_text("one")
            created = monitor.managed_create("thread-a", "event", str(file_path))
            receipt = monitor.ingest(created["binding"], {
                "id": "managed-event", "source": "managed/file", "type": "monitor.file.changed", "data": {},
            })
            errors = io.StringIO()
            with patch("codex_monitor.cli.SessionPool", return_value=Mock()), redirect_stderr(errors):
                self.assertEqual(main([
                    "--state", str(state), "reply", receipt["delivery_id"], "--id", "reply-1", "--message", "nope",
                ]), 2)
            self.assertIn("no external reply recipient", errors.getvalue())
            self.assertEqual(ReplyStore(state).pending("managed/file")["data"], [])


if __name__ == "__main__":
    unittest.main()
