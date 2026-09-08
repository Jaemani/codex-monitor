import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor.cli import main
from codex_monitor.monitor import Monitor
from codex_monitor.replies import ReplyStore


class SessionsTest(unittest.TestCase):
    def test_user_can_attach_observe_pause_and_reply_without_waking_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(*args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--state", tmp, *args]), 0)
                return out.getvalue()
            with patch("codex_monitor.session.Rpc", side_effect=AssertionError("no model calls")):
                run("init")
                run("source", "build")
                run("attach", "work", "--thread", "thread-user", "--source", "build")
                status = json.loads(run("sessions", "work", "--json"))
                self.assertFalse(status["receiver_running"])
                self.assertTrue(status["sessions"][0]["enabled"])
                run("pause", "work")
                self.assertIn("paused", run("sessions", "work"))
                run("unpause", "work")
                monitor = Monitor(tmp, None)
                receipt = monitor.ingest("work", {"id": "build7", "source": "build", "type": "build.failed", "data": {}})
                result = json.loads(run("reply", receipt["delivery_id"], "--id", "reply7", "--message", "I will check."))
                self.assertFalse(result["duplicate"])
                self.assertEqual(ReplyStore(tmp).pending("build")["data"][0]["message"], "I will check.")
                self.assertIn("build.failed", run("sessions"))
