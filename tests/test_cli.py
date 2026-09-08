import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from codex_monitor import cli
from codex_monitor.monitor import Monitor
from codex_monitor.session import Rpc, SharedLocalSession


class CLITest(unittest.TestCase):
    def test_init_source_binding_and_status_are_portable_and_do_not_start_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(*args):
                return subprocess.run([sys.executable, "-m", "codex_monitor", "--state", tmp, *args], capture_output=True, text=True)
            self.assertEqual(run("init").returncode, 0)
            self.assertNotEqual(run("init").returncode, 0)
            self.assertEqual(run("source", "build").returncode, 0)
            result = run("bind", "work", "--thread", "thread-user", "--source", "build", "--endpoint", "local")
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(run("status").stdout)
            self.assertEqual(status["bindings"][0]["thread"], "thread-user")
            self.assertEqual(status["events"], {})
            self.assertEqual(Path(tmp, "admin.token").stat().st_mode & 0o077, 0)
            result = run("doctor", "--endpoint", "unix:///nonexistent/codex-monitor-test.sock", "--surface", "desktop")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(result.stdout)["ready"])

    def test_inspect_reports_local_and_native_state_without_changing_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({
                "version": 1,
                "port": 8766,
                "sources": {},
                "limits": {},
            }))
            rpc = Rpc(
                command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py"))],
                timeout=.5,
            )
            session = SharedLocalSession(rpc)
            pool = mock.Mock()
            pool.side_effect = lambda _endpoint: session
            try:
                monitor = Monitor(root, lambda _endpoint: session)
                monitor.bind("work", "thread-user", "shared-local", ["build"])
                rpc.call("test/active", {"value": True})
                receipt = monitor.ingest("work", {
                    "id": "build-inspect",
                    "source": "build",
                    "type": "build.failed",
                    "data": {"job": 17},
                })
                self.assertTrue(monitor.dispatch_once())
                before = monitor.event(receipt["delivery_id"])
                methods_before = len(rpc.call("test/methods", {}))

                stdout = io.StringIO()
                with mock.patch("codex_monitor.cli.SessionPool", return_value=pool), redirect_stdout(stdout):
                    self.assertEqual(
                        cli.main(["--state", tmp, "inspect", receipt["delivery_id"]]),
                        0,
                    )
                queued = json.loads(stdout.getvalue())
                self.assertEqual(queued["local"]["state"], "accepted")
                self.assertEqual(queued["native"]["state"], "queued")
                self.assertTrue(queued["read_only"])
                self.assertEqual(monitor.event(receipt["delivery_id"]), before)

                rpc.call("test/active", {"value": False})
                stdout = io.StringIO()
                with mock.patch("codex_monitor.cli.SessionPool", return_value=pool), redirect_stdout(stdout):
                    self.assertEqual(
                        cli.main(["--state", tmp, "inspect", receipt["delivery_id"]]),
                        0,
                    )
                consumed = json.loads(stdout.getvalue())
                self.assertEqual(consumed["local"]["state"], "accepted")
                self.assertEqual(consumed["native"]["state"], "consumed")
                self.assertEqual(monitor.event(receipt["delivery_id"]), before)

                missing = monitor.ingest("work", {
                    "id": "build-inspect-missing",
                    "source": "build",
                    "type": "build.failed",
                    "data": {"job": 18},
                })
                missing_before = monitor.event(missing["delivery_id"])
                stdout = io.StringIO()
                with mock.patch("codex_monitor.cli.SessionPool", return_value=pool), redirect_stdout(stdout):
                    self.assertEqual(
                        cli.main(["--state", tmp, "inspect", missing["delivery_id"]]),
                        0,
                    )
                unknown = json.loads(stdout.getvalue())
                self.assertEqual(unknown["local"]["state"], "pending")
                self.assertEqual(unknown["native"]["state"], "unknown")
                self.assertEqual(monitor.event(missing["delivery_id"]), missing_before)

                methods_after = rpc.call("test/methods", {})[methods_before:]
                self.assertTrue(methods_after)
                self.assertLessEqual(
                    set(methods_after),
                    {"thread/queue/list", "thread/turns/list", "test/active", "test/methods"},
                )

                offline_pool = mock.Mock(side_effect=RuntimeError("native inspection unavailable"))
                stdout = io.StringIO()
                with mock.patch("codex_monitor.cli.SessionPool", return_value=offline_pool), redirect_stdout(stdout):
                    self.assertEqual(
                        cli.main(["--state", tmp, "inspect", receipt["delivery_id"]]),
                        0,
                    )
                unavailable = json.loads(stdout.getvalue())
                self.assertEqual(unavailable["local"]["state"], "accepted")
                self.assertEqual(unavailable["native"]["state"], "unknown")
                self.assertIn("unavailable", unavailable["native"]["reason"])
                self.assertEqual(monitor.event(receipt["delivery_id"]), before)
            finally:
                rpc.close()
