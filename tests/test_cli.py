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
    def test_connect_resumes_only_explicit_thread_on_selected_owner(self):
        class ExecCalled(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "codex_monitor.cli.os.execvp", side_effect=ExecCalled
        ) as execute, mock.patch("codex_monitor.cli.server_token", return_value=None):
            with self.assertRaises(ExecCalled):
                cli.main(["--state", tmp, "connect", "--endpoint", "ws://127.0.0.1:9010",
                          "--cwd", tmp, "--thread", "explicit-task"])
            execute.assert_called_once_with("codex", ["codex", "--remote", "ws://127.0.0.1:9010",
                                                       "-C", tmp, "resume", "explicit-task"])

    def test_connect_passes_auth_by_environment_name_without_token_in_argv(self):
        class ExecCalled(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}), mock.patch(
            "codex_monitor.cli.server_token", return_value="test-only-credential"
        ), mock.patch("codex_monitor.cli.os.execvp", side_effect=ExecCalled) as execute:
            with self.assertRaises(ExecCalled):
                cli.main(["--state", tmp, "connect", "--endpoint", "wss://owner.example",
                          "--cwd", tmp, "--thread", "explicit-task"])
            command = execute.call_args.args[1]
            self.assertNotIn("test-only-credential", command)
            self.assertEqual(os.environ["CODEX_MONITOR_SERVER_TOKEN"], "test-only-credential")
            self.assertIn("--remote-auth-token-env", command)

    def test_resident_needs_no_receiver_state_and_restores_signal_handlers(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "codex_monitor.cli.ResidentKeeper"
        ) as factory, mock.patch("codex_monitor.cli.signal.signal") as signals, mock.patch(
            "codex_monitor.cli.SessionPool", side_effect=AssertionError("no queue writer")
        ), redirect_stdout(io.StringIO()):
            keeper = factory.return_value
            keeper.status.return_value = {"connected": False}
            keeper.run.side_effect = lambda stopped: stopped.set()
            signals.return_value = "previous-handler"
            self.assertEqual(cli.main(["--state", str(Path(tmp) / "absent"), "resident",
                                       "--endpoint", "unix:///tmp/owned.sock", "--thread", "one",
                                       "--thread", "two"]), 0)
            self.assertFalse((Path(tmp) / "absent").exists())
            self.assertEqual(factory.call_args.args, ("unix:///tmp/owned.sock", ["one", "two"]))
            self.assertEqual(signals.call_count, 4)
            self.assertEqual(signals.call_args.args[1], "previous-handler")

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

    def test_conversation_metadata_cli_sets_and_lists_project_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(*args):
                return subprocess.run(
                    [sys.executable, "-m", "codex_monitor", "--state", tmp, *args],
                    capture_output=True, text=True,
                )

            self.assertEqual(run("init").returncode, 0)
            self.assertEqual(run("source", "build").returncode, 0)
            self.assertEqual(
                run("bind", "work", "--thread", "thread-user", "--source", "build").returncode,
                0,
            )
            result = run(
                "conversation", "set", "--thread", "thread-user",
                "--project", "Operations", "--name", "Deployments",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "thread": "thread-user", "project": "Operations", "display_name": "Deployments",
            })
            listed = run("conversation", "list")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout), {"conversations": [{
                "thread": "thread-user", "project": "Operations", "display_name": "Deployments",
            }]})

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
                self.assertEqual(queued["processing"]["state"], "awaiting_native_consumption")
                self.assertEqual(queued["processing"]["consumer_presence"], "unknown")
                self.assertFalse(queued["processing"]["automatic_replay_safe"])
                self.assertGreaterEqual(queued["processing"]["accepted_age_seconds"], 0)
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
                self.assertEqual(consumed["processing"]["state"], "native_consumed")
                self.assertFalse(consumed["processing"]["work_completion_verified"])
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
