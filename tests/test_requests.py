import concurrent.futures
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from codex_monitor.errors import IngressError
from codex_monitor.requests import RequestStore


class RequestStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    @staticmethod
    def parent(delivery="delivery-1", binding="binding-1", source="build"):
        return {"id": delivery, "binding": binding, "source": source}

    def test_parent_scoping_duplicate_and_immutable_ownership(self):
        store = RequestStore(self.temp.name)
        first = store.create_for_parent(
            "thread-a", self.parent(), "request-1", {"command": "build"}, expires_at=10,
        )
        self.assertEqual(first["state"], "received")
        self.assertEqual(first["revision"], 0)
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["notifications"], [])
        duplicate = store.create_for_parent(
            "thread-a", self.parent(), "request-1", {"command": "build"}, expires_at=10,
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["request_id"], first["request_id"])
        self.assertEqual(store.get_by_id(first["request_id"], conversation_id="thread-a", source="build")["request_id"], first["request_id"])
        with self.assertRaisesRegex(IngressError, "request not found"):
            store.get_by_id(first["request_id"], conversation_id="thread-b")
        with self.assertRaisesRegex(IngressError, "request not found"):
            store.get("thread-a", "other", "request-1")
        with self.assertRaises(IngressError) as caught:
            store.create_for_parent(
                "thread-a", self.parent(delivery="other-delivery"), "request-1", {"command": "build"}, expires_at=10,
            )
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(IngressError):
            store.create_for_parent("thread-a", {"id": "delivery-1", "source": "build"}, "request-2", {})
        with self.assertRaisesRegex(IngressError, "another conversation"):
            store.create_for_parent(
                "thread-b", {**self.parent(), "conversation_id": "thread-a"}, "request-2", {},
            )
        other = store.create_for_parent(
            "thread-b", self.parent(), "request-1", {"command": "build"}, expires_at=10,
        )
        self.assertNotEqual(other["request_id"], first["request_id"])

    def test_relative_expiry_is_persisted_and_duplicate_safe(self):
        now = [100.0]
        store = RequestStore(self.temp.name, clock=lambda: now[0])
        first = store.create_for_parent(
            "thread-a", self.parent(), "relative", {}, expires_in=10,
        )
        self.assertEqual(first["expires_in"], 10.0)
        self.assertEqual(first["expires_at"], 110.0)
        now[0] = 500.0
        duplicate = store.create_for_parent(
            "thread-a", self.parent(), "relative", {}, expires_in=10,
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["expires_at"], 110.0)
        with self.assertRaisesRegex(IngressError, "different immutable"):
            store.create_for_parent("thread-a", self.parent(), "relative", {}, expires_in=11)
        with self.assertRaisesRegex(IngressError, "mutually exclusive"):
            store.create_for_parent(
                "thread-a", self.parent(), "both", {}, expires_at=600, expires_in=10,
            )
        for invalid in (True, float("nan"), float("inf"), -1):
            with self.subTest(invalid=invalid), self.assertRaises(IngressError):
                store.create_for_parent("thread-a", self.parent(), "invalid-" + str(invalid), {}, expires_in=invalid)

    def test_transition_cas_append_only_and_terminal_no_regression(self):
        store = RequestStore(self.temp.name)
        created = store.create_for_parent("thread-a", self.parent(), "request-1", {"command": "build"})
        acknowledged = store.transition(
            "thread-a", "build", "request-1", update_id="ack-1",
            target_state="acknowledged", expected_revision=0,
        )
        self.assertEqual(acknowledged["state"], "acknowledged")
        self.assertEqual(acknowledged["notifications"], [])
        running = store.transition(
            "thread-a", "build", "request-1", update_id="run-1",
            target_state="in_progress", expected_revision=1, summary={"worker": "one"},
        )
        self.assertEqual(running["revision"], 2)
        self.assertEqual(len(running["notifications"]), 1)
        pending = store.pending_notifications(limit=10)
        self.assertEqual(len(pending), 1)
        notification = pending[0]
        self.assertEqual(notification["original_binding"], "binding-1")
        self.assertEqual(notification["source"], "build")
        self.assertEqual(notification["event_id"], notification["envelope"]["id"])
        self.assertEqual(notification["envelope"]["data"]["message"], "Request request-1: in_progress (revision 2).")
        self.assertNotIn("payload", notification["envelope"]["data"])
        replay = store.transition(
            "thread-a", "build", "request-1", update_id="run-1",
            target_state="in_progress", expected_revision=1, summary={"worker": "one"},
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(store.history("thread-a", "build", "request-1")), 2)
        completed = store.transition(
            "thread-a", "build", "request-1", update_id="done-1",
            target_state="completed", expected_revision=2,
        )
        self.assertEqual(completed["state"], "completed")
        with self.assertRaisesRegex(IngressError, "not allowed"):
            store.transition(
                "thread-a", "build", "request-1", update_id="late-1",
                target_state="failed", expected_revision=3,
            )
        accepted = store.notification_accepted(notification["notification_id"], "delivery-status-1")
        self.assertEqual(accepted["state"], "accepted")
        self.assertTrue(store.notification_accepted(notification["notification_id"], "delivery-status-1")["duplicate"])
        with self.assertRaises(IngressError):
            store.notification_accepted(notification["notification_id"], "different-delivery")

    def test_concurrent_cas_and_update_ids_are_request_scoped(self):
        store = RequestStore(self.temp.name)
        store.create_for_parent("thread-a", self.parent(), "request-a", {})
        store.create_for_parent("thread-b", self.parent(), "request-b", {})
        store.create_for_parent("thread-b", self.parent(), "request-c", {})
        barrier = threading.Barrier(2)

        def advance(thread, key, update_id):
            local = RequestStore(self.temp.name)
            barrier.wait()
            try:
                return local.transition(
                    thread, "build", key, update_id=update_id, target_state="acknowledged",
                    expected_revision=0,
                )
            except IngressError as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: advance(*args), (("thread-a", "request-a", "cas-1"), ("thread-a", "request-a", "cas-2"))))
        self.assertEqual(sum(isinstance(value, IngressError) for value in results), 1)
        self.assertEqual(store.get("thread-a", "build", "request-a")["revision"], 1)
        other = store.transition(
            "thread-b", "build", "request-b", update_id="same-key",
            target_state="acknowledged", expected_revision=0,
        )
        self.assertFalse(other["duplicate"])
        independent = store.transition(
            "thread-b", "build", "request-c", update_id="same-key",
            target_state="acknowledged", expected_revision=0,
        )
        self.assertFalse(independent["duplicate"])

    def test_expiry_is_explicit_durable_and_reserves_terminal_slot(self):
        store = RequestStore(self.temp.name, max_updates_per_request=2)
        created = store.create_for_parent(
            "thread-a", self.parent(), "request-expire", {}, expires_at=10,
        )
        store.transition(
            "thread-a", "build", "request-expire", update_id="ack-1",
            target_state="acknowledged", expected_revision=0,
        )
        with self.assertRaisesRegex(IngressError, "expiry slot"):
            store.transition(
                "thread-a", "build", "request-expire", update_id="run-1",
                target_state="in_progress", expected_revision=1,
            )
        self.assertEqual(store.expire_due(now=9), [])
        expired = store.expire_due(now=10)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["state"], "expired")
        self.assertEqual(expired[0]["revision"], 2)
        restarted = RequestStore(self.temp.name)
        current = restarted.get_by_id(created["request_id"], conversation_id="thread-a", source="build")
        self.assertEqual(current["state"], "expired")
        self.assertEqual(len(restarted.pending_notifications()), 1)

    def test_notification_revision_order_is_per_request_only(self):
        store = RequestStore(self.temp.name)
        first = store.create_for_parent("thread-a", self.parent(), "ordered-a", {})
        second = store.create_for_parent("thread-a", self.parent(), "ordered-b", {})
        running = store.transition(
            "thread-a", "build", "ordered-a", update_id="run-a",
            target_state="in_progress", expected_revision=0,
        )
        completed = store.transition(
            "thread-a", "build", "ordered-a", update_id="done-a",
            target_state="completed", expected_revision=1,
        )
        other = store.transition(
            "thread-a", "build", "ordered-b", update_id="run-b",
            target_state="in_progress", expected_revision=0,
        )
        pending = store.pending_notifications(limit=10)
        self.assertEqual(
            {row["notification_id"] for row in pending},
            {running["notifications"][0]["notification_id"], other["notifications"][0]["notification_id"]},
        )
        deferred = store.notification_deferred(running["notifications"][0]["notification_id"], "paused", retry_after=10)
        ready = store.pending_notifications(now=deferred["updated"])
        self.assertEqual([row["notification_id"] for row in ready], [other["notifications"][0]["notification_id"]])
        store.notification_accepted(other["notifications"][0]["notification_id"], "delivery-b")
        self.assertEqual(store.pending_notifications(now=deferred["updated"]), [])
        store.notification_accepted(running["notifications"][0]["notification_id"], "delivery-a")
        ready = store.pending_notifications(now=deferred["next_at"])
        self.assertEqual([row["notification_id"] for row in ready], [completed["notifications"][1]["notification_id"]])

    def test_concurrent_startup_migrates_legacy_columns_once(self):
        path = Path(self.temp.name) / "requests.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE requests (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL,
                source TEXT NOT NULL,
                request_key TEXT NOT NULL,
                original_delivery_id TEXT NOT NULL,
                original_binding TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                expires_at REAL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(conversation_id, source, request_key)
            );
            CREATE TABLE request_updates (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                expected_revision INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                previous_state TEXT NOT NULL,
                state TEXT NOT NULL,
                summary TEXT,
                created REAL NOT NULL,
                UNIQUE(request_id, update_id)
            );
            CREATE TABLE request_notifications (
                notification_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                update_id TEXT NOT NULL,
                original_binding TEXT NOT NULL,
                source TEXT NOT NULL,
                event_id TEXT UNIQUE NOT NULL,
                envelope TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0,
                delivery_id TEXT,
                error TEXT,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(request_id, update_id)
            );
            """
        )
        db.close()
        barrier = threading.Barrier(2)

        def open_store():
            barrier.wait()
            return RequestStore(self.temp.name)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(pool.map(lambda _: open_store(), (1, 2)))
        self.assertEqual(len(stores), 2)
        with closing(sqlite3.connect(path)) as check:
            request_columns = {row[1] for row in check.execute("PRAGMA table_info(requests)")}
            notification_columns = {row[1] for row in check.execute("PRAGMA table_info(request_notifications)")}
        self.assertIn("expires_in", request_columns)
        self.assertIn("revision", notification_columns)
        created = stores[0].create_for_parent("thread-a", self.parent(), "migrated", {})
        updated = stores[1].transition(
            "thread-a", "build", "migrated", update_id="run-1",
            target_state="in_progress", expected_revision=0,
        )
        self.assertEqual(updated["notifications"][0]["state"], "pending")

    def test_notification_migration_preserves_existing_transition_order(self):
        store = RequestStore(self.temp.name)
        store.create_for_parent("thread-a", self.parent(), "legacy", {})
        progress = store.transition("thread-a", "build", "legacy", update_id="run",
            target_state="in_progress", expected_revision=0)
        store.transition("thread-a", "build", "legacy", update_id="done",
            target_state="completed", expected_revision=1)
        notice = progress["notifications"][0]
        deferred = store.notification_deferred(notice["notification_id"], "paused", retry_after=60)
        with closing(sqlite3.connect(store.path)) as db:
            db.execute("ALTER TABLE request_notifications DROP COLUMN revision")
        restarted = RequestStore(self.temp.name)
        self.assertEqual(restarted.pending_notifications(now=deferred["updated"]), [])
        ready = restarted.pending_notifications(now=deferred["next_at"])
        self.assertEqual([item["notification_id"] for item in ready], [notice["notification_id"]])

    def test_expiry_and_json_limits_and_notification_capacity(self):
        store = RequestStore(self.temp.name, max_payload_bytes=20, max_update_bytes=20, max_notifications=3)
        with self.assertRaises(IngressError):
            store.create_for_parent("thread-a", self.parent(), "too-large", {"value": "x" * 20})
        with self.assertRaises(IngressError):
            store.create_for_parent("thread-a", self.parent(), "bad-nan", {"value": math.nan})
        created = store.create_for_parent("thread-a", self.parent(), "bounded", {}, expires_at=1)
        with self.assertRaises(IngressError):
            store.transition(
                "thread-a", "build", "bounded", update_id="summary-too-large",
                target_state="in_progress", expected_revision=0, summary={"value": "x" * 20},
            )
        running = store.transition(
            "thread-a", "build", "bounded", update_id="run-1",
            target_state="in_progress", expected_revision=0,
        )
        self.assertEqual(running["capacity"]["notifications_used"], 1)
        second = store.create_for_parent("thread-b", self.parent(), "bounded", {}, expires_at=1)
        with self.assertRaisesRegex(IngressError, "reserved"):
            store.create_for_parent("thread-c", self.parent(), "bounded", {}, expires_at=1)
        self.assertEqual(store.expire_due(now=2)[0]["state"], "expired")
        self.assertEqual(store.get_by_id(second["request_id"])["state"], "expired")

    def test_notification_retry_backoff_restart_and_listing_cursor(self):
        store = RequestStore(self.temp.name)
        for index in range(3):
            store.create_for_parent("thread-a", self.parent(), f"request-{index}", {})
        listed = store.list_requests("thread-a", limit=2)
        self.assertEqual(len(listed["data"]), 2)
        self.assertIsNotNone(listed["next"])
        self.assertEqual(len(store.list_requests("thread-a", limit=2, after=listed["next"])["data"]), 1)
        created = store.transition(
            "thread-a", "build", "request-0", update_id="run-1",
            target_state="in_progress", expected_revision=0,
        )
        notification = created["notifications"][0]
        second = store.transition(
            "thread-a", "build", "request-1", update_id="run-2",
            target_state="in_progress", expected_revision=0,
        )
        deferred = store.notification_deferred(notification["notification_id"], "binding paused", retry_after=10)
        self.assertEqual(deferred["attempts"], 0)
        ready = store.pending_notifications(now=deferred["updated"])
        self.assertEqual([row["notification_id"] for row in ready], [second["notifications"][0]["notification_id"]])
        store.notification_accepted(second["notifications"][0]["notification_id"], "delivery-2")
        failed = store.notification_error(notification["notification_id"], "temporary failure")
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(store.pending_notifications(now=failed["updated"]), [])
        restarted = RequestStore(self.temp.name)
        due = restarted.pending_notifications(now=failed["next_at"])
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["error"], "temporary failure")
        self.assertTrue(restarted.notification_accepted(due[0]["notification_id"], "delivery-accepted")["state"] == "accepted")


if __name__ == "__main__":
    unittest.main()
