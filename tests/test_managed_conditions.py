from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from codex_monitor.managed import ManagedSupervisor
from codex_monitor.monitor import Monitor


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class RecordingSession:
    def __init__(self):
        self.calls = []

    def deliver(self, thread, client_id, text):
        self.calls.append((thread, client_id, text))
        return {"submission_id": "submission-" + str(len(self.calls))}

    def reconcile(self, thread, client_id):
        return {"submission_id": "submission-1"} if any(
            call[1] == client_id for call in self.calls
        ) else None


class ManagedConditionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.session = RecordingSession()
        self.monitor = Monitor(self.root, lambda _endpoint: self.session, clock=self.clock)
        self.supervisors = []

    def tearDown(self):
        for supervisor in self.supervisors:
            supervisor.close()

    def supervisor(self, monitor=None):
        supervisor = ManagedSupervisor(
            monitor or self.monitor,
            poll_interval=.01,
            clock=self.clock,
            monotonic=self.clock,
            # Direct result application below keeps these policy tests
            # deterministic while still exercising the real supervisor.
            sampler=lambda *_args: None,
        )
        self.supervisors.append(supervisor)
        return supervisor

    @staticmethod
    def sample(path, marker):
        return {"path": str(path), "state": "present", "sha256": marker}

    def apply(self, supervisor, watch, marker):
        row = self.monitor.managed_runtime_row(watch["id"])
        worker = SimpleNamespace(watch_id=row["id"], lifecycle_epoch=row["lifecycle_epoch"])
        supervisor._apply_result(worker, row, self.sample(row["path"], marker), self.clock())

    def events(self):
        with self.monitor.connect() as db:
            ids = [row["id"] for row in db.execute("SELECT id FROM events ORDER BY seq")]
        return [self.monitor.event(event_id) for event_id in ids]

    def create(self, thread="thread-a", name="health", *, debounce=5):
        return self.monitor.managed_create(
            thread, name, str(self.root / (thread + "-" + name)),
            interval=.1, debounce_seconds=debounce,
        )

    def test_silent_baseline_and_stable_change_threshold(self):
        watch = self.create()
        supervisor = self.supervisor()

        self.apply(supervisor, watch, "healthy")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.monitor.managed_status("thread-a", "health")["last_sample"]["sha256"], "healthy")

        self.clock.value = 1
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.assertTrue(self.monitor.managed_status("thread-a", "health")["condition"]["pending"])
        self.clock.value = 4
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 6
        self.apply(supervisor, watch, "failed")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["envelope"]["data"]["current"]["sha256"], "failed")
        self.assertFalse(self.monitor.managed_status("thread-a", "health")["condition"]["pending"])

    def test_reverting_to_baseline_cancels_pending_change(self):
        watch = self.create()
        supervisor = self.supervisor()

        self.apply(supervisor, watch, "healthy")
        self.clock.value = 1
        self.apply(supervisor, watch, "failed")
        self.clock.value = 2
        self.apply(supervisor, watch, "healthy")
        self.assertFalse(self.monitor.managed_status("thread-a", "health")["condition"]["pending"])
        self.clock.value = 10
        self.apply(supervisor, watch, "failed")
        self.clock.value = 14
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 15
        self.apply(supervisor, watch, "failed")
        self.assertEqual(len(self.events()), 1)

    def test_restart_preserves_candidate_but_does_not_credit_downtime(self):
        watch = self.create()
        first = self.supervisor()
        self.apply(first, watch, "healthy")
        self.clock.value = 1
        self.apply(first, watch, "failed")

        self.clock.value = 3_601
        restarted = self.supervisor()
        self.apply(restarted, watch, "failed")
        self.assertEqual(self.events(), [])
        self.assertTrue(self.monitor.managed_status("thread-a", "health")["condition"]["pending"])
        self.clock.value = 3_605
        self.apply(restarted, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 3_606
        self.apply(restarted, watch, "failed")
        self.assertEqual(len(self.events()), 1)

    def test_pause_resume_epoch_restarts_observed_age(self):
        watch = self.create()
        supervisor = self.supervisor()
        self.apply(supervisor, watch, "healthy")
        self.clock.value = 1
        self.apply(supervisor, watch, "failed")

        self.monitor.managed_set_enabled("thread-a", "health", False)
        supervisor.poll_once()
        self.monitor.managed_set_enabled("thread-a", "health", True)
        self.clock.value = 100
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 104
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 105
        self.apply(supervisor, watch, "failed")
        self.assertEqual(len(self.events()), 1)

    def test_failed_ingest_replays_old_pending_before_new_condition(self):
        watch = self.create()
        supervisor = self.supervisor()
        self.apply(supervisor, watch, "healthy")

        self.clock.value = 1
        self.apply(supervisor, watch, "failed")
        self.clock.value = 6
        original_ingest = self.monitor.ingest
        failed = [True]

        def fail_once(binding, envelope):
            if failed[0]:
                failed[0] = False
                raise OSError("simulated ingest outage")
            return original_ingest(binding, envelope)

        self.monitor.ingest = fail_once
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        status = self.monitor.managed_status("thread-a", "health")
        self.assertTrue(status["checkpoint_pending"])
        self.assertTrue(status["condition"]["pending"])

        self.clock.value = 7
        self.apply(supervisor, watch, "new")
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["envelope"]["data"]["current"]["sha256"], "failed")
        condition = self.monitor.managed_status("thread-a", "health")["condition"]
        self.assertTrue(condition["pending"])
        self.assertEqual(condition["candidate"]["sample"]["sha256"], "new")

    def test_same_name_is_scoped_to_two_threads(self):
        first = self.create("thread-a", "health")
        second = self.create("thread-b", "health")
        supervisor = self.supervisor()

        self.apply(supervisor, first, "healthy-a")
        self.apply(supervisor, second, "healthy-b")
        self.clock.value = 1
        self.apply(supervisor, first, "failed-a")
        self.assertTrue(self.monitor.managed_status("thread-a", "health")["condition"]["pending"])
        self.assertFalse(self.monitor.managed_status("thread-b", "health")["condition"]["pending"])
        self.clock.value = 6
        self.apply(supervisor, first, "failed-a")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["binding"], first["binding"])
        self.assertEqual(self.monitor.managed_status("thread-b", "health")["last_sample"]["sha256"], "healthy-b")

    def test_status_reports_condition_pending_and_corrupt_state(self):
        watch = self.create()
        supervisor = self.supervisor()
        self.apply(supervisor, watch, "healthy")
        self.clock.value = 1
        self.apply(supervisor, watch, "failed")
        status = self.monitor.managed_status("thread-a", "health")
        self.assertIsNone(status["condition_error"])
        self.assertTrue(status["condition"]["pending"])

        condition_path = self.root / "managed" / (watch["id"] + ".condition.json")
        condition_path.write_text('{"version": 1, "candidate": {"sample": 1}}')
        status = self.monitor.managed_status("thread-a", "health")
        self.assertIsNone(status["condition"])
        self.assertIn("timestamp", status["condition_error"])

    def test_failed_observation_does_not_count_as_stable_time(self):
        watch = self.create()
        supervisor = self.supervisor()
        self.apply(supervisor, watch, "healthy")
        self.clock.value = 1
        self.apply(supervisor, watch, "failed")
        row = self.monitor.managed_runtime_row(watch["id"])
        supervisor._sample_error(row, "file sample: worker_timeout")
        self.clock.value = 100
        self.apply(supervisor, watch, "failed")
        self.assertEqual(self.events(), [])
        self.clock.value = 105
        self.apply(supervisor, watch, "failed")
        self.assertEqual(len(self.events()), 1)


if __name__ == "__main__":
    unittest.main()
