import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_monitor.managed import ManagedSupervisor
from codex_monitor.monitor import Monitor


def hang_sample(*args):
    time.sleep(60)


class SampleLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / "sample"
        self.path.write_text("one")
        self.monitor = Monitor(root / "state", lambda _: None)
        self.monitor.managed_create("thread-a", "sample", str(self.path), .1)
        self.supervisor = ManagedSupervisor(self.monitor, poll_interval=.01)
        self.addCleanup(self.supervisor.close)

    def assert_reaped(self, pid):
        # Unlike poll()/active_children(), this assertion cannot silently reap
        # a leaked zombie and turn the lifecycle failure into a passing test.
        with self.assertRaises(ChildProcessError):
            result = os.waitpid(pid, os.WNOHANG)
            self.fail(f"child was not reaped by its owner: {result}")

    def test_early_interpreter_exit_is_reaped_and_stops_further_spawns(self):
        children = []

        def broken_runtime(*args):
            child = subprocess.Popen(["/usr/bin/false"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            children.append(child)
            time.sleep(.05)  # The real child exits before registration.
            return child

        with patch("codex_monitor.managed.launch", side_effect=broken_runtime) as launch:
            for _ in range(20):
                self.supervisor.poll_once()
                time.sleep(.01)
        self.assertEqual(launch.call_count, 1)
        self.assertFalse(self.supervisor._workers)
        self.assert_reaped(children[0].pid)
        status = self.monitor.managed_status("thread-a", "sample")
        self.assertIn("spawning suspended", status["last_sample_error"])

    def test_spawn_oserror_opens_one_global_circuit_for_all_watches(self):
        for i in range(12):
            self.monitor.managed_create(f"thread-{i}", "sample", str(self.path), .1)
        with patch("codex_monitor.managed.launch", side_effect=BlockingIOError(35, "no process slots")) as launch:
            for _ in range(10):
                self.supervisor.poll_once()
        self.assertEqual(launch.call_count, 1)
        self.assertFalse(self.supervisor._workers)

    def test_closed_supervisor_cannot_admit_new_workers(self):
        self.supervisor.close()
        with patch("codex_monitor.managed.launch") as launch:
            self.supervisor.poll_once()
        launch.assert_not_called()

    def test_malformed_result_is_reaped_and_opens_circuit(self):
        children = []

        def malformed(*args):
            child = subprocess.Popen([sys.executable, "-c", "print('{}')"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            children.append(child)
            return child

        with patch("codex_monitor.managed.launch", side_effect=malformed) as launch:
            deadline = time.monotonic() + 3
            while not self.supervisor._spawn_error and time.monotonic() < deadline:
                self.supervisor.poll_once()
                time.sleep(.01)
            self.supervisor.poll_once()
        self.assertTrue(self.supervisor._spawn_error)
        self.assertEqual(launch.call_count, 1)
        self.assert_reaped(children[0].pid)

    def test_ownership_setup_exception_reaps_already_spawned_child(self):
        children = []
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with patch("codex_monitor.sample_worker.subprocess.Popen", side_effect=record), \
                patch("codex_monitor.managed.SampleResult", side_effect=RuntimeError("setup failed")):
            self.supervisor.poll_once()
        self.assertEqual(len(children), 1)
        self.assert_reaped(children[0].pid)

    def test_timeout_and_close_reap_real_sampler(self):
        supervisor = ManagedSupervisor(self.monitor, sampler=hang_sample, sample_timeout=.1)
        self.addCleanup(supervisor.close)
        supervisor.poll_once()
        pid = next(iter(supervisor._workers.values())).process.pid
        time.sleep(.15)
        supervisor._reap_workers(time.time())
        supervisor.close()
        self.assertFalse(supervisor._workers)
        self.assert_reaped(pid)

    def test_receiver_death_closes_worker_lifetime_lease(self):
        code = '''
import sys, time
from pathlib import Path
from codex_monitor.managed import ManagedSupervisor
from codex_monitor.monitor import Monitor
from tests.test_sample_lifecycle import hang_sample
root = Path(sys.argv[1])
m = Monitor(root / "orphan-state", lambda _: None)
m.managed_create("thread", "sample", str(root / "sample"), .1)
s = ManagedSupervisor(m, sampler=hang_sample, sample_timeout=60)
s.poll_once()
print(next(iter(s._workers.values())).process.pid, flush=True)
time.sleep(60)
'''
        parent = subprocess.Popen([sys.executable, "-c", code, self.tmp.name], stdout=subprocess.PIPE, text=True)
        pid = None
        try:
            pid = int(parent.stdout.readline())
            time.sleep(.15)
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(.05)
            self.fail("sampler survived receiver death")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent.stdout.close()
            if pid:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
