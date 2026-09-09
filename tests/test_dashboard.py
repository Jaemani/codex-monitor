import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from codex_monitor.dashboard import (
    DashboardError,
    DashboardReader,
    _binding_rows,
    _color_enabled,
    _conversation_label,
    _cycle_route,
    _launch_selected,
    _preferred_route,
    _ro_connect,
    _selected_binding_index,
    render_text,
)
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
        self.assertIn("Enter", rendered)
        self.assertNotIn("bad", rendered)
        details = render_text(snapshot, width=100, height=12, detail=True)
        self.assertIn("bad", details)

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
        self.assertIn("Live", compact)
        self.assertIn("1 conversation  /  3 connections", compact)
        self.assertIn("Conversation", compact)
        self.assertIn("Connections", compact)
        self.assertIn("Recent activity", compact)
        self.assertIn("1 conversation  /  3 connections", compact)
        self.assertIn("1 pending", compact)
        self.assertNotIn("ON", compact)
        self.assertNotIn("OFF", compact)
        self.assertNotIn("UNKNOWN", compact)
        self.assertNotIn("uuid-on", compact)
        self.assertNotIn("shared-local", compact)
        self.assertNotIn("thread-a", compact)
        self.assertIn("\x1b[32m", compact)
        self.assertIn("\x1b[48;5;236m", compact)

        details = render_text(snapshot, width=120, height=20, color=False, detail=True, selected=0, now=1_001)
        self.assertIn("uuid-on", details)
        self.assertIn("Details: on-binding", details)
        self.assertIn("endpoint shared-local", details)
        self.assertIn("Status: ● enabled ○ paused ◐ stale · unavailable", details)
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
        last_details = render_text(many, width=100, height=20, color=False, detail=True, selected=9, now=1_001)
        self.assertIn("Route 1/1 · binding-9", last_details)
        self.assertIn("Details: binding-9", last_details)
        short = render_text(many, width=100, height=8, color=False, selected=9, now=1_001)
        self.assertLessEqual(len(short.splitlines()), 8)
        self.assertIn("Route 1/1 · binding-9", short)
        self.assertIn("Enter open", short)
        for width in (24, 36):
            narrow = render_text(many, width=width, height=8, selected=9)
            self.assertLessEqual(len(narrow.splitlines()), 8)
            self.assertTrue(all(len(line) <= width for line in narrow.splitlines()))
            self.assertIn("q", narrow.splitlines()[-1])
            self.assertIn("binding-9", narrow)

    def test_compact_render_uses_human_managed_name_and_hides_generated_binding_id(self):
        snapshot = {
            "ok": True,
            "read_only": True,
            "generated_at": 1_000,
            "receiver": {"process_alive": True, "ready": True, "status_checked": True},
            "connections": [{
                "thread": "thread-a",
                "bindings": [{
                    "name": "managed-0123456789abcdef",
                    "enabled": True,
                    "endpoint": "wss://owner.example:8765",
                    "sources": ["managed/file"],
                    "events": {"counts": {}, "latest": None},
                }],
                "events": {},
                "requests": {"available": False, "reason": "none"},
                "collectors": [{
                    "name": "deploy-check",
                    "binding": "managed-0123456789abcdef",
                    "enabled": True,
                    "worker_seen_status": "fresh",
                }],
            }],
        }
        rendered = render_text(snapshot, width=100, height=12, color=False, live=True, now=1_001)
        self.assertIn("deploy-check", rendered)
        self.assertNotIn("managed-0123456789abcdef", rendered)

    def test_conversation_rows_cycle_routes_and_launch_the_visible_route(self):
        snapshot = {
            "ok": True,
            "read_only": True,
            "generated_at": 1_000,
            "receiver": {"process_alive": True, "ready": True, "status_checked": True},
            "connections": [{
                "thread": "thread-a",
                "bindings": [
                    {"name": "local", "enabled": True, "endpoint": "shared-local", "sources": ["build"],
                     "identity_exact": True, "events": {"counts": {}, "latest": None}},
                    {"name": "mobile", "enabled": True, "endpoint": "wss://owner.example:8765", "sources": ["mobile"],
                     "identity_exact": True, "events": {"counts": {"accepted": 1}, "latest": {"state": "accepted", "age_seconds": 2}}},
                    {"name": "backup", "enabled": False, "endpoint": "unix:///tmp/codex.sock", "sources": ["backup"],
                     "identity_exact": True, "events": {"counts": {}, "latest": None}},
                ],
                "events": {},
                "requests": {"available": False, "reason": "none"},
                "collectors": [],
            }],
        }
        connection = snapshot["connections"][0]
        preferred = _preferred_route(connection)
        self.assertEqual(preferred, 1)
        self.assertEqual(_preferred_route({"bindings": [
            {"enabled": True, "endpoint": "shared-local"},
            {"enabled": False, "endpoint": "wss://owner.example:8765"},
        ]}), 0)
        self.assertEqual(_cycle_route(connection, preferred), 2)
        self.assertEqual(_cycle_route(connection, 2), 0)
        rendered = render_text(snapshot, width=90, height=18, selected=0, selected_route=preferred, live=True)
        self.assertIn("Route 2/3 · mobile", rendered)
        self.assertIn("1 sent to Codex", rendered)
        self.assertIn("▌", rendered)
        self.assertNotIn("wss://owner.example:8765", rendered)
        self.assertNotIn("\x1b", rendered)
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        reader = mock.Mock()
        reader.selected_binding.side_effect = lambda name, thread, endpoint: {
            "name": name, "thread": thread, "endpoint": endpoint,
        }
        with mock.patch("codex_monitor.dashboard.server_token", return_value=None):
            status = _launch_selected(
                reader, snapshot, _selected_binding_index(snapshot, 0, preferred),
                mock.Mock(), mock.Mock(), runner=runner,
            )
        self.assertEqual(status, "Codex exited successfully")
        self.assertEqual(calls[0][0], ["codex", "--remote", "wss://owner.example:8765", "resume", "thread-a"])

    def test_conversation_label_strips_only_a_matching_source_prefix(self):
        connection = {
            "bindings": [
                {"name": "discord-uate-flow", "sources": ["discord-uate"]},
                {"name": "discord-uate-flow-cli", "sources": ["discord-uate"]},
            ],
            "collectors": [],
        }
        self.assertEqual(_conversation_label(connection), "flow")
        self.assertEqual(_conversation_label({"bindings": [{"name": "PM", "sources": ["pm"]}]}), "PM")

    def test_color_policy_honors_no_color_and_dumb_terminal(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        self.assertFalse(_color_enabled("auto", stream, {"TERM": "dumb"}))
        self.assertFalse(_color_enabled("auto", stream, {"TERM": "xterm-256color", "NO_COLOR": "1"}))
        self.assertFalse(_color_enabled("never", stream, {"TERM": "xterm-256color"}))
        self.assertTrue(_color_enabled("always", stream, {"TERM": "dumb", "NO_COLOR": "1"}))

    def _set_binding_endpoint(self, endpoint):
        db = sqlite3.connect(self.root / "monitor.sqlite3")
        try:
            db.execute("UPDATE bindings SET endpoint=? WHERE name=?", (endpoint, "work"))
            db.commit()
        finally:
            db.close()

    def test_selected_binding_launches_exact_thread_endpoint_with_auth_and_restores_terminal(self):
        self._set_binding_endpoint("ws://127.0.0.1:8765")
        snapshot = DashboardReader(self.root).snapshot()
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        lifecycle = []

        def suspend():
            lifecycle.append("suspend")

        def restore():
            lifecycle.append("restore")

        with mock.patch.dict(os.environ, {"CODEX_MONITOR_SERVER_TOKEN": "existing-token"}, clear=False), \
                mock.patch("codex_monitor.dashboard.server_token", return_value="test-only-credential"):
            status = _launch_selected(
                DashboardReader(self.root), snapshot, 0, suspend, restore, runner=runner
            )

        self.assertEqual(status, "Codex exited successfully")
        self.assertEqual(lifecycle, ["suspend", "restore"])
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command, [
            "codex", "--remote-auth-token-env", "CODEX_MONITOR_SERVER_TOKEN",
            "--remote", "ws://127.0.0.1:8765", "resume", "thread-a",
        ])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["env"]["CODEX_MONITOR_SERVER_TOKEN"], "test-only-credential")
        self.assertNotIn("test-only-credential", command)

    def test_selected_binding_rejects_stale_display_identity_before_runner(self):
        self._set_binding_endpoint("ws://127.0.0.1:8765")
        snapshot = DashboardReader(self.root).snapshot()
        self._set_binding_endpoint("ws://127.0.0.1:8766")
        runner = mock.Mock()
        with self.assertRaisesRegex(DashboardError, "identity changed"):
            _launch_selected(DashboardReader(self.root), snapshot, 0, mock.Mock(), mock.Mock(), runner=runner)
        runner.assert_not_called()

    def test_selected_binding_restores_terminal_after_child_interrupt(self):
        self._set_binding_endpoint("ws://127.0.0.1:8765")
        snapshot = DashboardReader(self.root).snapshot()
        lifecycle = []

        def runner(*_args, **_kwargs):
            lifecycle.append("runner")
            raise KeyboardInterrupt

        status = _launch_selected(
            DashboardReader(self.root), snapshot,
            0,
            lambda: lifecycle.append("suspend"),
            lambda: lifecycle.append("restore"),
            runner=runner,
        )
        self.assertEqual(status, "Codex interrupted")
        self.assertEqual(lifecycle, ["suspend", "runner", "restore"])

    def test_selected_binding_rejects_unsafe_identity_collision(self):
        db = sqlite3.connect(self.root / "monitor.sqlite3")
        try:
            db.execute(
                "INSERT INTO bindings(name,thread,endpoint,sources) VALUES(?,?,?,?)",
                ("unsafe\x1b", "thread-a", "ws://127.0.0.1:8765", '["build"]'),
            )
            db.execute(
                "INSERT INTO bindings(name,thread,endpoint,sources) VALUES(?,?,?,?)",
                ("unsafe ", "thread-a", "ws://127.0.0.1:8766", '["build"]'),
            )
            db.commit()
        finally:
            db.close()
        snapshot = DashboardReader(self.root).snapshot()
        rows = [
            (index, binding)
            for index, (_, binding, _) in enumerate(_binding_rows(snapshot))
            if binding["name"] == "unsafe "
        ]
        unsafe_index = next(index for index, binding in rows if binding["endpoint"].endswith(":8765"))
        with self.assertRaisesRegex(DashboardError, "identity is unsafe"):
            _launch_selected(DashboardReader(self.root), snapshot, unsafe_index, mock.Mock(), mock.Mock(), runner=mock.Mock())

    def test_selected_binding_rejects_unsupported_endpoints(self):
        for endpoint in ("shared-local", "local", "ssh://owner", "ws://owner.example:8765", "ws://127.0.0.1:bad"):
            with self.subTest(endpoint=endpoint):
                self._set_binding_endpoint(endpoint)
                snapshot = DashboardReader(self.root).snapshot()
                runner = mock.Mock()
                with self.assertRaises(DashboardError):
                    _launch_selected(DashboardReader(self.root), snapshot, 0, mock.Mock(), mock.Mock(), runner=runner)
                runner.assert_not_called()

    def test_open_failure_notice_keeps_shared_local_reason_visible(self):
        snapshot = DashboardReader(self.root).snapshot()
        rendered = render_text(
            snapshot,
            width=80,
            height=8,
            notice="shared-local has no owner address; configure an explicit owner endpoint",
        )
        self.assertIn("shared-local has no owner address; configure an explicit owner endpoint", rendered)
        self.assertLessEqual(len(rendered.splitlines()), 8)

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
