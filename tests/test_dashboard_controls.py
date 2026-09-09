import sqlite3
from pathlib import Path
import tempfile
import unittest

from codex_monitor.dashboard import DashboardError, DashboardReader, _action_target, render_text
from codex_monitor.errors import IngressError
from codex_monitor.monitor import Monitor


class DashboardControlsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.monitor = Monitor(self.root, lambda _endpoint: None)
        self.monitor.bind("owner-route", "thread-a", "ws://127.0.0.1:8765", ["owner"])
        self.monitor.bind("shared-route", "thread-a", "shared-local", ["shared"])
        self.monitor.bind("other-route", "thread-b", "shared-local", ["other"])

    def _read(self, query, params=()):
        db = sqlite3.connect(self.root / "monitor.sqlite3")
        try:
            return db.execute(query, params).fetchall()
        finally:
            db.close()

    def _binding(self, name):
        columns = {row[1] for row in self._read("PRAGMA table_info(bindings)")}
        selected = ["name", "thread", "endpoint", "enabled"]
        if "removed" in columns:
            selected.append("removed")
        row = self._read(
            "SELECT " + ",".join(selected) + " FROM bindings WHERE name=?",
            (name,),
        )
        return dict(zip(selected, row[0])) if row else None

    def test_metadata_keeps_projects_distinct_when_display_names_match(self):
        self.monitor.set_conversation_metadata("thread-a", "project-alpha", "Inbox")
        self.monitor.set_conversation_metadata("thread-b", "project-beta", "Inbox")
        rows = self._read(
            "SELECT thread,project,display_name FROM conversation_metadata ORDER BY thread"
        )
        self.assertEqual(rows, [
            ("thread-a", "project-alpha", "Inbox"),
            ("thread-b", "project-beta", "Inbox"),
        ])

    def test_pause_resume_and_remove_target_one_exact_route(self):
        receipt = self.monitor.ingest("shared-route", {
            "id": "dashboard-controls-event",
            "source": "shared",
            "type": "controls.test",
            "data": {"value": "preserve"},
        })
        self.assertTrue(receipt["delivery_id"])

        self.monitor.dashboard_action("shared-route", "thread-a", "shared-local", "pause")
        self.assertEqual(self._binding("shared-route")["enabled"], 0)
        self.assertEqual(self._binding("owner-route")["enabled"], 1)
        self.assertEqual(self._binding("other-route")["enabled"], 1)

        self.monitor.dashboard_action("shared-route", "thread-a", "shared-local", "resume")
        self.assertEqual(self._binding("shared-route")["enabled"], 1)

        with self.assertRaises(IngressError):
            self.monitor.dashboard_action("shared-route", "thread-a", "ws://127.0.0.1:8765", "pause")
        self.assertEqual(self._binding("shared-route")["enabled"], 1)

        before_events = self._read("SELECT count(*) FROM events WHERE binding=?", ("shared-route",))[0][0]
        self.monitor.dashboard_action("shared-route", "thread-a", "shared-local", "remove")
        after_events = self._read("SELECT count(*) FROM events WHERE binding=?", ("shared-route",))[0][0]
        self.assertEqual(after_events, before_events)
        removed = self._binding("shared-route")
        if removed is None:
            self.assertIsNone(removed)
        else:
            self.assertEqual(removed.get("removed"), 1)
        self.assertIsNotNone(self._binding("owner-route"))
        self.assertIsNotNone(self._binding("other-route"))

    def test_reader_groups_metadata_and_targets_exact_route(self):
        self.monitor.bind("same-name-route", "thread-c", "shared-local", ["same"])
        self.monitor.set_conversation_metadata("thread-a", "project-alpha", "Inbox")
        self.monitor.set_conversation_metadata("thread-b", "project-beta", "Inbox")
        self.monitor.set_conversation_metadata("thread-c", "project-alpha", "Inbox")

        snapshot = DashboardReader(self.root).snapshot()
        self.assertTrue(snapshot["ok"])
        connections = snapshot["connections"]
        by_thread = {connection["thread"]: connection for connection in connections}
        self.assertEqual(by_thread["thread-a"]["project"], "project-alpha")
        self.assertEqual(by_thread["thread-b"]["project"], "project-beta")
        self.assertEqual(by_thread["thread-c"]["project"], "project-alpha")
        labels = {connection["thread"]: connection["display_name"] for connection in connections}
        self.assertEqual(labels["thread-b"], "Inbox")
        self.assertEqual(labels["thread-a"], "Inbox")
        self.assertEqual(labels["thread-c"], "Inbox")
        rendered = render_text(snapshot, width=100, height=24)
        self.assertIn("project-alpha", rendered)
        self.assertIn("project-beta", rendered)
        self.assertIn("Inbox · thread-a", rendered)
        self.assertIn("Inbox · thread-c", rendered)

        selected = next(index for index, item in enumerate(connections) if item["thread"] == "thread-a")
        target = _action_target(snapshot, selected, 1)
        self.assertEqual(target, {
            "binding": "shared-route", "thread": "thread-a", "endpoint": "shared-local",
        })
        by_thread["thread-a"]["bindings"][1]["identity_exact"] = False
        with self.assertRaises(DashboardError):
            _action_target(snapshot, selected, 1)

        self.monitor.dashboard_action("shared-route", "thread-a", "shared-local", "remove")
        refreshed = DashboardReader(self.root).snapshot()
        refreshed_names = {
            binding["name"]
            for connection in refreshed["connections"]
            for binding in connection["bindings"]
        }
        self.assertNotIn("shared-route", refreshed_names)
        self.assertIn("owner-route", refreshed_names)
        self.assertIn("other-route", refreshed_names)


if __name__ == "__main__":
    unittest.main()
