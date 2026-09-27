import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from codex_monitor.resource_metrics import ResourceSampler, process_tree, storage, project_resources
from codex_monitor.dashboard_board import build, project_resource_line


class ResourceMetricsTest(unittest.TestCase):
    def test_only_receiver_descendants_are_counted(self):
        value = process_tree("10 1 S 100 1.5\n11 10 Z 0 0\n12 11 S 20 2\n99 1 S 900 90", 10)
        self.assertEqual(value["processes"], 3)
        self.assertEqual(value["zombies"], 1)
        self.assertEqual(value["rss_bytes"], 120 * 1024)
        self.assertEqual(value["cpu_percent"], 3.5)
        with self.assertRaises(ValueError):
            process_tree("99 1 S 1 0", 10)

    def test_storage_does_not_follow_links_and_reports_partial_scans_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor.sqlite3").write_bytes(b"1234")
            (root / "receiver.log").write_bytes(b"12")
            (root / "link").symlink_to(root / "receiver.log")
            result = storage(root)
            self.assertEqual(result["total_bytes"], 6)
            self.assertEqual(result["database_bytes"], 4)
            self.assertEqual(result["log_bytes"], 2)
            self.assertFalse(storage(root, limit=1)["available"])

    def test_sampling_is_cached_and_failures_do_not_become_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = Mock(return_value=0)
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "10 1 S 100 1"))
            sampler = ResourceSampler(temp, clock=clock, runner=runner)
            first = sampler.sample({"pid": 10})
            self.assertNotIn("change_bytes_per_hour", first["storage"])
            clock.return_value = 2
            self.assertEqual(sampler.sample({"pid": 10})["age_seconds"], 2)
            self.assertEqual(runner.call_count, 1)
            clock.return_value = 31
            runner.side_effect = subprocess.TimeoutExpired("ps", 1)
            value = sampler.sample({"pid": 10})
            self.assertFalse(value["process"]["available"])
            self.assertNotIn("zombies", value["process"])
            self.assertEqual(runner.call_args.kwargs["timeout"], 1)

    def test_resource_alert_visible_even_in_short_terminal(self):
        snapshot = {"ok": True, "connections": [], "resources": {
            "process": {"available": True, "processes": 3, "zombies": 2, "consecutive_zombie_samples": 2, "cpu_percent": 1, "rss_bytes": 100},
            "storage": {"available": False, "reason": "unavailable"}}}
        self.assertIn("RESOURCE ALERT", build(snapshot, width=80, height=24).render())
        self.assertIn("Unknown", project_resource_line(snapshot, "Test"))
        self.assertNotIn("MONITOR RESOURCES", build(snapshot, width=100, height=40).render())

    def test_transient_zombie_does_not_trigger_persistent_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = Mock(return_value=0)
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "10 1 S 100 1\n11 10 Z 0 0"))
            sampler = ResourceSampler(temp, clock=clock, runner=runner)
            first = sampler.sample({"pid": 10})
            self.assertEqual(first["process"]["consecutive_zombie_samples"], 1)
            self.assertNotIn("RESOURCE ALERT", build({"ok": True, "resources": first}, width=100, height=40).render())
            clock.return_value = 31
            self.assertEqual(sampler.sample({"pid": 10})["process"]["consecutive_zombie_samples"], 2)
            clock.return_value = 62
            runner.return_value = subprocess.CompletedProcess([], 0, "10 1 S 100 1")
            self.assertEqual(sampler.sample({"pid": 10})["process"]["consecutive_zombie_samples"], 0)

    def test_project_attribution_excludes_shared_receiver_and_other_projects(self):
        runtime = {"pid": 10, "sampler": {"available": True, "owned_workers": [
            {"pid": 11, "thread": "a"}, {"pid": 12, "thread": "b"}]}}
        values = project_resources("10 1 S 9999 90\n11 10 S 100 2\n12 10 S 200 3",
                                   runtime, [{"thread": "a", "project": "A"}, {"thread": "b", "project": "B"}])
        self.assertEqual(values['A']['rss_bytes'], 100 * 1024)
        self.assertEqual(values['A']['cpu_percent'], 2)
        self.assertEqual(values['B']['rss_bytes'], 200 * 1024)
        missing = project_resources("11 99 S 100 2", runtime, [{"thread": "a", "project": "A"}])
        self.assertFalse(missing['A']['available'])

    def test_dashboard_shows_project_resources_without_shared_cpu_panel(self):
        snapshot = {"ok": True, "connections": [{"thread": "a", "project": "A", "bindings": []}],
                    "resources": {"process": {"cpu_percent": 99}, "projects": {
                        "A": {"available": True, "workers": 2, "rss_bytes": 1024, "cpu_percent": 3}}}}
        text = build(snapshot, width=120, height=40).render()
        self.assertIn("Monitor: 2 workers", text)
        self.assertIn("CPU 3%", text)
        self.assertNotIn("99", text)
        self.assertNotIn("MONITOR RESOURCES", text)
