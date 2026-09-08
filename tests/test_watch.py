from pathlib import Path
import tempfile
import unittest

from codex_monitor.watch import ChangeWatcher


class WatchTest(unittest.TestCase):
    def test_change_only_events_and_lost_ack_reuse_id_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            sent = []
            watcher = ChangeWatcher(path, "health", "monitor.changed", lambda e: sent.append(e))
            watcher.check({"state": "healthy"})
            watcher.check({"state": "healthy"})
            self.assertEqual(sent, [])
            watcher.check({"state": "failed"})
            self.assertEqual(len(sent), 1)
            def fail(event):
                sent.append(event)
                raise OSError("lost receipt")
            watcher = ChangeWatcher(path, "health", "monitor.changed", fail)
            with self.assertRaises(OSError): watcher.check({"state": "healthy"})
            pending_id = sent[-1]["id"]
            watcher = ChangeWatcher(path, "health", "monitor.changed", lambda e: sent.append(e))
            watcher.check({"state": "healthy"})
            self.assertEqual(sent[-1]["id"], pending_id)
            watcher.check({"state": "healthy"})
            self.assertEqual(len(sent), 3)
