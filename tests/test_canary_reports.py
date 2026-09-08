import contextlib
import json
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    module_name = "test_" + path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanaryReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.json"
        self.python = Path(sys.executable).resolve()

    def run_main(self, module, *argv):
        with mock.patch.object(sys, "argv", [module.__file__, *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def fake_latency(self, *, cleanup_error=False):
        instances = []

        class FakeCanary:
            def __init__(self, args):
                self.args = args
                self.report = {"result": "RUNNING", "samples_complete": True}
                self.timeline = []
                instances.append(self)

            def run(self):
                self.timeline.append(("run", self.report["result"]))

            def cleanup(self):
                self.timeline.append(("cleanup", self.report["result"]))
                if cleanup_error:
                    raise RuntimeError("cleanup failed")

            def finish(self):
                self.timeline.append(("finish", self.report["result"]))

        return FakeCanary, instances

    def fake_predicate(self, *, cleanup_error=False, tui_result=None):
        instances = []

        class FakeCanary:
            def __init__(self, args):
                self.args = args
                self.report = {"result": "RUNNING", "steps": []}
                self.timeline = []
                self.save_states = []
                instances.append(self)

            def save(self):
                self.save_states.append(self.report["result"])

            def setup(self):
                self.timeline.append(("setup", self.report["result"]))

            def run(self):
                self.timeline.append(("run", self.report["result"]))

            def run_soak(self):
                self.timeline.append(("soak", self.report["result"]))

            def run_tui(self):
                self.timeline.append(("tui", self.report["result"]))
                self.report["tui"] = {"result": tui_result}

            def cleanup(self):
                self.timeline.append(("cleanup", self.report["result"]))
                if cleanup_error:
                    raise RuntimeError("cleanup failed")

            def finish(self):
                self.timeline.append(("finish", self.report["result"]))

        return FakeCanary, instances

    def test_latency_success_stays_running_until_cleanup_then_passes(self):
        module = load_script("latency-canary.py")
        fake, instances = self.fake_latency()
        with mock.patch.object(module, "Canary", fake):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report), "--samples", "5", "--timeout", "10",
            )
        self.assertEqual(result, 0)
        self.assertEqual(instances[0].timeline, [
            ("run", "RUNNING"), ("cleanup", "RUNNING"), ("finish", "PASS"),
        ])

    def test_latency_cleanup_failure_is_final_fail(self):
        module = load_script("latency-canary.py")
        fake, instances = self.fake_latency(cleanup_error=True)
        with mock.patch.object(module, "Canary", fake):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report), "--samples", "5", "--timeout", "10",
            )
        self.assertEqual(result, 2)
        self.assertEqual(instances[0].timeline, [
            ("run", "RUNNING"), ("cleanup", "RUNNING"), ("finish", "FAIL"),
        ])
        self.assertEqual(instances[0].report["cleanup_error"], "RuntimeError: cleanup failed")

    def test_predicate_success_stays_running_until_cleanup_then_passes(self):
        module = load_script("managed-predicate-canary.py")
        fake, instances = self.fake_predicate()
        with mock.patch.object(module, "Canary", fake):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report),
            )
        self.assertEqual(result, 0)
        self.assertEqual(instances[0].timeline, [
            ("setup", "RUNNING"), ("run", "RUNNING"), ("soak", "RUNNING"),
            ("cleanup", "RUNNING"), ("finish", "PASS"),
        ])

    def test_predicate_cleanup_failure_is_final_fail(self):
        module = load_script("managed-predicate-canary.py")
        fake, instances = self.fake_predicate(cleanup_error=True)
        with mock.patch.object(module, "Canary", fake):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report),
            )
        self.assertEqual(result, 1)
        self.assertEqual(instances[0].timeline[-2:], [
            ("cleanup", "RUNNING"), ("finish", "FAIL"),
        ])
        self.assertEqual(instances[0].report["cleanup_error"], "RuntimeError: cleanup failed")

    def test_predicate_required_tui_skip_is_incomplete_not_pass(self):
        module = load_script("managed-predicate-canary.py")
        fake, instances = self.fake_predicate(tui_result="SKIPPED")
        with mock.patch.object(module, "Canary", fake):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report), "--tui",
            )
        self.assertEqual(result, 1)
        self.assertEqual(instances[0].report["result"], "INCOMPLETE")
        self.assertEqual(instances[0].timeline[-2:], [
            ("cleanup", "RUNNING"), ("finish", "INCOMPLETE"),
        ])

    def test_latency_cleanup_attempts_all_resources_and_finishes_fail(self):
        module = load_script("latency-canary.py")
        canary_args = SimpleNamespace(report=self.report, samples=5, python=self.python)
        canary = module.Canary(canary_args)
        canary.run = lambda: canary.report.update(samples_complete=True)

        class Terminal:
            def __init__(self):
                self.closed = False

            def pump(self, _seconds):
                raise RuntimeError("capture failed")

            def close(self):
                self.closed = True

        class Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Rpc:
            def __init__(self):
                self.closed = False

            def call(self, *_args):
                raise RuntimeError("archive failed")

            def close(self):
                self.closed = True

        class RemoteServer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        terminal = Terminal()
        stream = Stream()
        rpc = Rpc()
        remote_server = RemoteServer()
        work = Path(self.temp.name) / "owned-work"
        work.mkdir()
        canary.terminal = terminal
        canary.receiver_logs = [stream]
        canary.rpc = rpc
        canary.thread = "thread-user"
        canary.remote_server = remote_server
        canary.work = work
        stopped = []
        canary.stop_receiver = lambda **_kwargs: stopped.append(True)

        with mock.patch.object(module, "Canary", return_value=canary):
            result = self.run_main(
                module, "--run", "--python", str(self.python),
                "--report", str(self.report), "--samples", "5", "--timeout", "10",
            )

        self.assertEqual(result, 2)
        self.assertTrue(terminal.closed)
        self.assertTrue(stream.closed)
        self.assertTrue(rpc.closed)
        self.assertTrue(remote_server.closed)
        self.assertEqual(stopped, [True])
        self.assertFalse(work.exists())
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved["result"], "FAIL")
        self.assertIn("capture_terminal", saved["cleanup_error"])
        self.assertIn("archive_owned_thread", saved["cleanup_error"])
        self.assertTrue(saved["temporary_work_removed"])


if __name__ == "__main__":
    unittest.main()
