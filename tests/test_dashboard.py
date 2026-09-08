import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from codex_monitor.dashboard import DashboardReader, _color_enabled, _ro_connect, render_text
from codex_monitor.lock import ProcessLock
from codex_monitor.monitor import Monitor
from codex_monitor import cli


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.monitor = Monitor(self.root, lambda _: None)
        self.monitor.bind("work", "thread-a", "shared-local", ["build"])
        self.receipt = self.monitor.ingest("work", {
            "id": "build-1",
            "source": "build",
            "type": "build.failed",
            "data": {"message": "worker"},
        })

    def test_snapshot_is_read_only_and_contains_connection_inventory(self):
        before = (self.root / "monitor.sqlite3").stat().st_mtime_ns
        snapshot = DashboardReader(self.root, clock=lambda: 10_000).snapshot()
        self.assertTrue(snapshot["ok"])
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["connections"][0]["thread"], "thread-a")
        binding = snapshot["connections"][0]["bindings"][0]
        self.assertEqual(binding["events"]["counts"], {"pending": 1})
        self.assertEqual(binding["events"]["latest"]["delivery_id"], self.receipt["delivery_id"])
        self.assertEqual(snapshot["connections"][0]["requests"]["available"], False)
        after = (self.root / "monitor.sqlite3").stat().st_mtime_ns
        self.assertEqual(before, after)

        # URI mode=ro is a real SQLite read-only connection, not merely a
        # convention in the dashboard code.
        db = _ro_connect(self.root / "monitor.sqlite3")
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("CREATE TABLE dashboard_write_probe (x INTEGER)")

    def test_thread_filter_and_missing_or_corrupt_database_are_honest(self):
        self.monitor.bind("other", "thread-b", "shared-local", ["deploy"])
        filtered = DashboardReader(self.root, thread="thread-b").snapshot()
        self.assertEqual([item["thread"] for item in filtered["connections"]], ["thread-b"])

        (self.root / "monitor.sqlite3").unlink()
        missing = DashboardReader(self.root).snapshot()
        self.assertFalse(missing["ok"])
        self.assertIn("missing", missing["error"])

        (self.root / "monitor.sqlite3").write_bytes(b"not sqlite")
        corrupt = DashboardReader(self.root).snapshot()
        self.assertFalse(corrupt["ok"])
        self.assertIn("corrupt", corrupt["error"])

        (self.root / "monitor.sqlite3").unlink()
        db = sqlite3.connect(self.root / "monitor.sqlite3")
        db.executescript("CREATE TABLE bindings (name TEXT); CREATE TABLE events (id TEXT);")
        db.close()
        incompatible = DashboardReader(self.root).snapshot()
        self.assertFalse(incompatible["ok"])
        self.assertIn("corrupt", incompatible["error"])

    def test_render_removes_terminal_controls_and_supports_viewport_scrolling(self):
        snapshot = {
            "ok": True,
            "read_only": True,
            "receiver": {"process_alive": False, "ready": False, "reason": "bad\x1b[31m\nreason"},
            "connections": [{
                "thread": "thread\x1b[2J",
                "bindings": [{
                    "name": "work", "enabled": True, "endpoint": "shared-local", "sources": ["build"],
                    "events": {"counts": {"pending": 1}, "latest": {"state": "pending", "delivery_id": "d", "age_seconds": 1}},
                }],
                "events": {"counts": {"pending": 1}, "latest": None},
                "requests": {"available": False, "reason": "none"},
                "collectors": [],
            }],
        }
        rendered = render_text(snapshot, width=36, height=5, scroll=1)
        self.assertNotIn("\x1b", rendered)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 36)
        self.assertIn("scroll", rendered)

    def test_compact_render_groups_connections_and_keeps_details_explicit(self):
        snapshot = {
            "ok": True,
            "read_only": True,
            "generated_at": 1_000,
            "receiver": {"process_alive": True, "ready": True, "status_checked": True},
            "connections": [{
                "thread": "thread-a",
                "bindings": [
                    {"name": "on-binding", "enabled": True, "endpoint": "shared-local", "sources": ["build"],
                     "events": {"counts": {"pending": 1}, "latest": {"state": "pending", "delivery_id": "uuid-on", "age_seconds": 1}}},
                    {"name": "paused-binding", "enabled": False, "endpoint": "shared-local", "sources": ["build"],
                     "events": {"counts": {}, "latest": None}},
                    {"name": "unknown-binding", "enabled": None, "endpoint": "shared-local", "sources": [],
                     "events": {"counts": {}, "latest": None}},
                ],
                "events": {"counts": {}, "latest": None},
                "requests": {"available": False, "reason": "none"},
                "collectors": [],
            }],
        }
        compact = render_text(snapshot, width=120, height=20, color=True, live=True, frame=True, now=1_001)
        self.assertIn("LIVE VIEW", compact)
        self.assertIn("Last refreshed", compact)
        self.assertIn("Conversation: thread-a", compact)
        self.assertIn("ON", compact)
        self.assertIn("OFF", compact)
        self.assertIn("UNKNOWN", compact)
        self.assertNotIn("uuid-on", compact)
        self.assertIn("\x1b[32m", compact)
        self.assertIn("\x1b[31m", compact)
        self.assertIn("\x1b[33m", compact)

        details = render_text(snapshot, width=120, height=20, color=False, detail=True, selected=0, now=1_001)
        self.assertIn("uuid-on", details)
        self.assertIn("Details: on-binding", details)
        self.assertNotIn("\x1b", details)

        many = dict(snapshot)
        many["connections"] = [{
            "thread": f"thread-{index}",
            "bindings": [{"name": f"binding-{index}", "enabled": True, "endpoint": "shared-local", "sources": [],
                           "events": {"counts": {}, "latest": None}}],
            "events": {"counts": {}, "latest": None},
            "requests": {"available": False, "reason": "none"},
            "collectors": [],
        } for index in range(10)]
        last_details = render_text(many, width=100, height=8, color=False, detail=True, selected=9, now=1_001)
        self.assertIn("Conversation: thread-9", last_details)
        self.assertIn("Details: binding-9", last_details)

    def test_color_policy_honors_no_color_and_dumb_terminal(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        self.assertFalse(_color_enabled("auto", stream, {"TERM": "dumb"}))
        self.assertFalse(_color_enabled("auto", stream, {"TERM": "xterm-256color", "NO_COLOR": "1"}))
        self.assertFalse(_color_enabled("never", stream, {"TERM": "xterm-256color"}))
        self.assertTrue(_color_enabled("always", stream, {"TERM": "dumb", "NO_COLOR": "1"}))

    def _status_server(self, status=200, body=None, location=None):
        seen = []
        payload = body if body is not None else {"bindings": [], "events": {}, "capabilities": {}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                if payload is not None:
                    self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, seen

    def _probe(self, server):
        (self.root / "config.json").write_text(json.dumps({"version": 1, "port": server.server_port}))
        (self.root / "admin.token").write_text("probe-secret\n")
        with ProcessLock(self.root / "serve.lock"):
            return DashboardReader(self.root).snapshot()

    def test_authenticated_status_shape_and_worker_error_are_visible_without_token_output(self):
        server, seen = self._status_server(body={
            "bindings": [], "events": {}, "capabilities": {"request_lifecycle": True},
            "worker_error": "private worker detail",
        })
        snapshot = self._probe(server)
        self.assertTrue(snapshot["receiver"]["ready"])
        self.assertEqual(snapshot["receiver"]["health"], "degraded")
        self.assertEqual(seen[0]["authorization"], "Bearer probe-secret")
        self.assertNotIn("probe-secret", json.dumps(snapshot))

    def test_status_rejects_redirects_and_invalid_shape_without_following_or_echoing_secret(self):
        destination, destination_seen = self._status_server()
        redirect, redirect_seen = self._status_server(status=302, body=None, location=f"http://127.0.0.1:{destination.server_port}/v1/status")
        snapshot = self._probe(redirect)
        self.assertFalse(snapshot["receiver"]["ready"])
        self.assertIn("HTTP 302", snapshot["receiver"]["reason"])
        self.assertEqual(len(destination_seen), 0)
        self.assertNotIn("probe-secret", json.dumps(snapshot))
        self.assertEqual(redirect_seen[0]["authorization"], "Bearer probe-secret")

        malformed, _ = self._status_server(body={"ready": True})
        invalid = self._probe(malformed)
        self.assertFalse(invalid["receiver"]["ready"])
        self.assertIn("status probe failed", invalid["receiver"]["reason"])

    def test_status_unauthorized_is_not_treated_as_ready(self):
        server, _ = self._status_server(status=401, body={"error": "admin credentials required"})
        snapshot = self._probe(server)
        self.assertTrue(snapshot["receiver"]["status_checked"])
        self.assertFalse(snapshot["receiver"]["ready"])
        self.assertEqual(snapshot["receiver"]["reason"], "status endpoint returned HTTP 401")

    def test_cli_dashboard_does_not_construct_monitor(self):
        output = io.StringIO()
        with mock.patch.object(cli, "Monitor", side_effect=AssertionError("dashboard constructed Monitor")):
            with redirect_stdout(output):
                self.assertEqual(cli.main(["--state", str(self.root), "dashboard", "--once", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
