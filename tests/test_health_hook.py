import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_monitor.monitor import Monitor
from codex_monitor.http import Server


SPEC = importlib.util.spec_from_file_location("health_hook", Path(__file__).resolve().parents[1] / "examples/health-hook.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class HealthHookTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "example launcher process check uses POSIX")
    def test_probe_process_to_authenticated_receiver(self):
        class Handler(BaseHTTPRequestHandler):
            status = 200

            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        class Sink:
            def deliver(self, *args):
                return {"submission_id": "local-test-sink"}

            def reconcile(self, *args):
                return None

        health = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        health_worker = threading.Thread(target=health.serve_forever, daemon=True)
        health_worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                monitor = Monitor(root / "receiver", lambda _: Sink())
                monitor.bind("ops", "existing-thread", "shared-local", ["health"])
                receiver = Server(monitor, {"health": "local-test-source"}, "local-test-admin", port=0).start()
                try:
                    token = monitor.root / "source-health.token"
                    token.write_text("local-test-source")
                    (monitor.root / "config.json").write_text(json.dumps({"version": 1,
                        "port": int(receiver.url.rsplit(":", 1)[1]),
                        "sources": {"health": {"token_file": str(token)}}}))
                    launcher = root / "monitor-cli"
                    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -m codex_monitor \"$@\"\n")
                    launcher.chmod(0o700)
                    command = [sys.executable, str(Path(hook.__file__)), "--url",
                        f"http://127.0.0.1:{health.server_port}/health", "--checkpoint", str(root / "probe.db"),
                        "--monitor-bin", str(launcher), "--monitor-state", str(monitor.root),
                        "--binding", "ops", "--source", "health", "--failures", "2"]

                    def invoke():
                        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                                cwd=Path(__file__).resolve().parents[1])
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, "")

                    for _ in range(3):
                        invoke()
                    with monitor.connect() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                    Handler.status = 503
                    for _ in range(4):
                        invoke()
                    Handler.status = 200
                    invoke()
                    with monitor.connect() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
                finally:
                    receiver.close()
        finally:
            health.shutdown()
            health.server_close()
            health_worker.join(timeout=2)

    def test_real_http_checks_are_quiet_until_outage_and_deduplicate_retries(self):
        class Handler(BaseHTTPRequestHandler):
            status = 200

            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            monitor = Monitor(root / "receiver", lambda _: self.fail("probe must not invoke Codex"))
            monitor.bind("ops", "existing-thread", "shared-local", ["health"])
            args = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/health", timeout=1,
                failures=3, checkpoint=str(root / "probe.sqlite3"), monitor_bin="/test/codex-monitor",
                monitor_state=str(root / "receiver"), binding="ops", source="health")
            attempts = []

            def submit(command, **kwargs):
                import json
                event = {"id": command[command.index("--id") + 1], "source": "health",
                         "type": "service.unhealthy", "data": json.loads(command[command.index("--data") + 1])}
                receipt = monitor.ingest("ops", event)
                attempts.append(receipt)
                # Simulate acknowledgement loss on the first accepted event.
                return SimpleNamespace(returncode=2 if len(attempts) == 1 else 0)

            with patch.object(hook.subprocess, "run", side_effect=submit):
                for _ in range(5):
                    self.assertEqual(hook.run(args), 0)
                self.assertEqual(attempts, [])
                Handler.status = 503
                self.assertEqual(hook.run(args), 0)
                self.assertEqual(hook.run(args), 0)
                self.assertEqual(attempts, [])
                self.assertEqual(hook.run(args), 2)
                self.assertEqual(hook.run(args), 0)
                self.assertEqual(attempts[0]["delivery_id"], attempts[1]["delivery_id"])
                for _ in range(5):
                    self.assertEqual(hook.run(args), 0)
                self.assertEqual(len(attempts), 2, "confirmed send must stop retransmitting during the same outage")
                Handler.status = 200
                for _ in range(3):
                    self.assertEqual(hook.run(args), 0)
                self.assertEqual(len(attempts), 2, "recovery and healthy checks must not send events")
                Handler.status = 503
                for _ in range(3):
                    self.assertEqual(hook.run(args), 0)
                self.assertNotEqual(attempts[-1]["delivery_id"], attempts[0]["delivery_id"])
                with monitor.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 2)
                args.binding = "different-target"
                with self.assertRaisesRegex(ValueError, "different check"):
                    hook.run(args)

    def test_invalid_url_and_timeout_do_not_initialize_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            args = SimpleNamespace(url="https://user:secret@example.com/", timeout=1,
                failures=3, checkpoint=str(Path(temp) / "checkpoint"), monitor_bin="/test/codex-monitor")
            with self.assertRaises(ValueError):
                hook.run(args)
            args.url, args.timeout = "http://127.0.0.1:99999/", 1
            with self.assertRaises(ValueError):
                hook.run(args)
            self.assertFalse(Path(args.checkpoint).exists())
            self.assertFalse(Path(args.checkpoint).exists())
            args.url, args.timeout = "http://127.0.0.1/", float("nan")
            with self.assertRaises(ValueError):
                hook.run(args)
