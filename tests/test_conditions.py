import json
from pathlib import Path
import tempfile
import unittest

from codex_monitor.conditions import ConditionDebouncer, ConditionStateError


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


class ConditionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "conditions.json"
        self.clock = FakeClock()

    def policy(self, seconds=5):
        return ConditionDebouncer(self.path, seconds, clock=self.clock)

    def test_first_baseline_is_immediate_but_policy_does_not_commit_it(self):
        policy = self.policy()

        self.assertEqual(policy.select({"state": "healthy"}, None), {"state": "healthy"})
        self.assertEqual(json.loads(self.path.read_text()), {"version": 1, "candidate": None})
        self.assertFalse(policy.commit({"state": "healthy"}))

    def test_stable_candidate_persists_across_restart_until_commit(self):
        sample = {"state": "failed", "attempt": 2}
        baseline = {"state": "healthy", "attempt": 2}
        policy = self.policy()

        self.assertIsNone(policy.select(sample, baseline))
        self.assertEqual(json.loads(self.path.read_text())["candidate"], {
            "sample": sample, "first_seen": 0.0, "last_seen": 0.0, "pause_epoch": 0,
        })

        self.clock.value = 4
        policy = self.policy()
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 8
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 9
        self.assertEqual(policy.select(sample, baseline), sample)
        # Selection is repeatable after a downstream crash or lost receipt.
        self.assertEqual(policy.select(sample, baseline), sample)
        self.assertTrue(policy.commit(sample))
        self.assertEqual(json.loads(self.path.read_text())["candidate"], None)

    def test_default_zero_is_immediate_and_failed_downstream_replays(self):
        sample = {"state": "failed"}
        baseline = {"state": "healthy"}
        policy = self.policy(0)

        self.assertEqual(policy.select(sample, baseline), sample)
        restarted = self.policy(0)
        self.assertEqual(restarted.select(sample, baseline), sample)
        self.assertTrue(restarted.commit(sample))

    def test_oscillation_and_reversion_cancel_candidate(self):
        baseline = {"state": "healthy"}
        first = {"state": "failed"}
        second = {"state": "unknown"}
        policy = self.policy()

        self.assertIsNone(policy.select(first, baseline))
        self.clock.value = 4
        self.assertIsNone(policy.select(second, baseline))
        self.clock.value = 8
        self.assertIsNone(policy.select(second, baseline))
        self.assertIsNone(policy.select(baseline, baseline))
        self.clock.value = 20
        self.assertIsNone(policy.select(second, baseline))
        self.assertEqual(json.loads(self.path.read_text())["candidate"]["first_seen"], 20.0)

    def test_clock_rollback_reanchors_age_without_premature_selection(self):
        sample = {"state": "failed"}
        baseline = {"state": "healthy"}
        policy = self.policy(10)

        self.clock.value = 0
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 8
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 5
        self.assertIsNone(policy.select(sample, baseline))
        self.assertEqual(json.loads(self.path.read_text())["candidate"]["first_seen"], 5.0)
        self.assertEqual(json.loads(self.path.read_text())["candidate"]["last_seen"], 5.0)
        self.clock.value = 9
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 14
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 15
        self.assertEqual(policy.select(sample, baseline), sample)

    def test_pause_epoch_resets_age_and_reset_discards_candidate(self):
        sample = {"state": "failed"}
        baseline = {"state": "healthy"}
        policy = self.policy()

        self.assertIsNone(policy.select(sample, baseline, pause_epoch=0))
        self.clock.value = 100
        self.assertIsNone(policy.select(sample, baseline, pause_epoch=1))
        self.clock.value = 104
        self.assertIsNone(policy.select(sample, baseline, pause_epoch=1))
        self.clock.value = 105
        self.assertEqual(policy.select(sample, baseline, pause_epoch=1), sample)
        self.assertTrue(policy.reset())
        self.assertFalse(policy.reset())

    def test_restart_preserves_candidate_but_does_not_credit_unobserved_downtime(self):
        sample = {"state": "failed"}
        baseline = {"state": "healthy"}
        policy = self.policy()

        self.clock.value = 10
        self.assertIsNone(policy.select(sample, baseline))
        self.clock.value = 3_610
        restarted = self.policy()
        self.assertIsNone(restarted.select(sample, baseline))
        state = json.loads(self.path.read_text())["candidate"]
        self.assertEqual(state["sample"], sample)
        self.assertEqual(state["first_seen"], 3_610.0)
        self.assertEqual(state["last_seen"], 3_610.0)
        self.clock.value = 3_614
        self.assertIsNone(restarted.select(sample, baseline))
        self.clock.value = 3_615
        self.assertEqual(restarted.select(sample, baseline), sample)

    def test_status_is_read_only_and_reports_pending_candidate(self):
        policy = self.policy()
        self.assertEqual(policy.status(), {"pending": False, "candidate": None})
        sample = {"state": "failed"}
        baseline = {"state": "healthy"}
        self.assertIsNone(policy.select(sample, baseline))
        before = self.path.read_bytes()
        status = policy.status()
        self.assertTrue(status["pending"])
        self.assertEqual(status["candidate"]["sample"], sample)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_state_is_an_error_and_is_not_silently_discarded(self):
        self.path.write_text('{"version": 1, "candidate": {"sample": 1}}')
        with self.assertRaises(ConditionStateError):
            self.policy().select(1, 0)
        self.assertIn('"sample": 1', self.path.read_text())

    def test_missing_baseline_does_not_discard_pending_candidate(self):
        sample = {"state": "failed"}
        self.assertIsNone(self.policy().select(sample, {"state": "healthy"}))
        with self.assertRaises(ConditionStateError):
            self.policy().select({"state": "other"}, None)
        self.assertEqual(json.loads(self.path.read_text())["candidate"]["sample"], sample)

    def test_commit_rejects_a_different_sample(self):
        baseline = {"state": "healthy"}
        sample = {"state": "failed"}
        self.assertIsNone(self.policy().select(sample, baseline))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.policy().commit({"state": "unknown"})


if __name__ == "__main__":
    unittest.main()
