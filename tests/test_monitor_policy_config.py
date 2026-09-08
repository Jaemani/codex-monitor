import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_monitor.errors import IngressError
from codex_monitor.monitor import Monitor


class PolicyConfigTest(unittest.TestCase):
    def test_policy_persists_and_lifecycle_epoch_invalidates_prior_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = Monitor(root, Mock())
            first = monitor.managed_create("thread-a", "build", str(root / "build"), debounce_seconds=3)
            second = monitor.managed_create("thread-b", "build", str(root / "other"))
            epoch = monitor.managed_runtime_row(first["id"])["lifecycle_epoch"]
            monitor.managed_set_enabled("thread-a", "build", False)
            monitor.managed_set_enabled("thread-a", "build", True)
            reopened = Monitor(root, Mock())
            self.assertEqual(reopened.managed_status("thread-a", "build")["debounce_seconds"], 3)
            self.assertEqual(reopened.managed_runtime_row(first["id"])["lifecycle_epoch"], epoch + 2)
            self.assertEqual(reopened.managed_runtime_row(second["id"])["lifecycle_epoch"], 0)
            reopened.managed_remove("thread-a", "build")
            self.assertEqual(reopened.managed_runtime_row(first["id"])["lifecycle_epoch"], epoch + 3)
            replacement = reopened.managed_create("thread-a", "build", str(root / "build"))
            self.assertNotEqual(first["id"], replacement["id"])
            self.assertEqual(replacement["debounce_seconds"], 0)

    def test_invalid_debounce_creates_no_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(Path(directory), Mock())
            for value in [-1, True, float("nan"), float("inf"), 86401, "3"]:
                with self.subTest(value=value), self.assertRaises(IngressError):
                    monitor.managed_create("thread-a", "build", str(Path(directory) / "build"), debounce_seconds=value)
            self.assertEqual(monitor.managed_status("thread-a")["monitors"], [])

    def test_concurrent_open_migrates_existing_registry_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = Monitor(root, Mock())
            monitor.managed_create("thread-a", "build", str(root / "build"))
            with closing(sqlite3.connect(monitor.path)) as db:
                db.execute("ALTER TABLE managed_watches DROP COLUMN lifecycle_epoch")
                db.execute("ALTER TABLE managed_watches DROP COLUMN debounce_seconds")
                db.commit()
            barrier = threading.Barrier(4)

            def reopen(_):
                barrier.wait()
                return Monitor(root, Mock()).managed_status("thread-a", "build")

            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(reopen, range(4)))
            self.assertTrue(all(row["debounce_seconds"] == 0 for row in rows))
            self.assertEqual({row["id"] for row in rows}, {rows[0]["id"]})
