import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from codex_monitor import cli
from codex_monitor.errors import IngressError
from codex_monitor.managed import ManagedSupervisor, safe_json_sample
from codex_monitor.monitor import Monitor
from codex_monitor.predicates import PredicateError, evaluate, normalize_condition, parse_document


class RecordingSession:
    def __init__(self):
        self.calls = []

    def deliver(self, thread, client_id, text):
        self.calls.append((thread, client_id, text))
        return {"submission_id": "submission-" + str(len(self.calls))}


class PredicateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_strict_json_types_and_bounded_pointer(self):
        self.assertEqual(
            evaluate({"value": True}, {"pointer": "/value", "operator": "eq", "value": 1}),
            {"state": "not_matched"},
        )
        self.assertEqual(
            evaluate({"value": True}, {"pointer": "/value", "operator": "ne", "value": 1}),
            {"state": "matched"},
        )
        with self.assertRaises(PredicateError):
            normalize_condition({"pointer": "/value", "operator": "gt", "value": True})
        with self.assertRaises(PredicateError):
            normalize_condition({"pointer": "/value", "operator": "eq", "value": {1: "not-json"}})
        with self.assertRaises(PredicateError):
            normalize_condition({"pointer": "/" + "x" * 1024, "operator": "eq", "value": 1})
        self.assertEqual(
            evaluate({"a/b": {"~key": ["ok"]}}, {
                "pointer": "/a~1b/~0key/0", "operator": "eq", "value": "ok",
            }),
            {"state": "matched"},
        )
        self.assertEqual(
            evaluate({"items": ["ok"]}, {
                "pointer": "/items/٠", "operator": "eq", "value": "ok",
            }),
            {"state": "invalid", "error": "missing_pointer"},
        )
        numeric_cases = (
            ("gt", 2, 1, "matched"), ("gte", 2, 2, "matched"),
            ("lt", 1, 2, "matched"), ("lte", 2, 2, "matched"),
            ("gt", 1, 2, "not_matched"),
        )
        for operator, actual, expected, state in numeric_cases:
            with self.subTest(operator=operator):
                self.assertEqual(
                    evaluate({"value": actual}, {"pointer": "/value", "operator": operator, "value": expected}),
                    {"state": state},
                )
        self.assertEqual(
            evaluate({"value": None}, {"pointer": "/value", "operator": "eq", "value": None}),
            {"state": "matched"},
        )
        self.assertEqual(
            evaluate({"nested": True}, {"pointer": "", "operator": "eq", "value": {"nested": True}}),
            {"state": "matched"},
        )
        self.assertEqual(
            evaluate({}, {"pointer": "/missing", "operator": "ne", "value": "anything"}),
            {"state": "invalid", "error": "missing_pointer"},
        )
        with self.assertRaises(PredicateError):
            normalize_condition({"pointer": "/value~2", "operator": "eq", "value": 1})
        with self.assertRaises(PredicateError):
            normalize_condition({"pointer": "/" + "/".join(["x"] * 33), "operator": "eq", "value": 1})

    def test_nonfinite_and_large_integer_documents_are_safe(self):
        with self.assertRaises(PredicateError):
            parse_document(b'{"value": 1e999}')
        with self.assertRaises(PredicateError):
            parse_document(b'{"nested": {"value": -1e999}}')
        huge = int("9" * 400)
        document = parse_document((json.dumps({"value": huge})).encode())
        self.assertEqual(
            evaluate(document, {"pointer": "/value", "operator": "eq", "value": huge}),
            {"state": "matched"},
        )

    def test_safe_json_sample_redacts_document_and_handles_invalid_inputs(self):
        path = self.root / "sample.json"
        path.write_text(json.dumps({"build": {"status": "failed"}}))
        sample = safe_json_sample(
            path, 1024, 1, {"pointer": "/build/status", "operator": "eq", "value": "failed"},
        )
        self.assertEqual(sample["predicate"], {"state": "matched"})
        self.assertNotIn("failed", json.dumps(sample))
        path.write_text("not-json")
        self.assertEqual(safe_json_sample(
            path, 1024, 1, {"pointer": "/build/status", "operator": "eq", "value": "failed"},
        )["error"], "invalid_json")

    def test_predicate_state_debounces_transitions_without_hash_resets(self):
        now = [0.0]
        session = RecordingSession()
        monitor = Monitor(self.root, lambda _endpoint: session, clock=lambda: now[0])
        path = self.root / "build.json"
        watch = monitor.managed_create(
            "thread-a", "build", str(path), debounce_seconds=5,
            condition={"pointer": "/status", "operator": "eq", "value": "failed"},
        )
        supervisor = ManagedSupervisor(
            monitor, poll_interval=.01, clock=lambda: now[0], monotonic=lambda: now[0],
            sampler=lambda *_args: None,
        )
        worker = SimpleNamespace(watch_id=watch["id"], lifecycle_epoch=0)

        def apply(state, digest):
            row = monitor.managed_runtime_row(watch["id"])
            supervisor._apply_result(worker, row, {
                "path": str(path), "state": "present", "sha256": digest,
                "predicate": {"state": state},
            }, now[0])

        apply("not_matched", "baseline")
        now[0] = 1
        apply("matched", "first")
        now[0] = 4
        apply("matched", "unrelated-file-edit")
        self.assertEqual(session.calls, [])
        now[0] = 6
        apply("matched", "third-edit")
        matched_id = monitor.managed_status("thread-a", "build")["last_delivery"]["delivery_id"]
        events = monitor.event(matched_id)
        self.assertEqual(len(session.calls), 0)
        self.assertEqual(events["envelope"]["data"]["current"], {"condition": "matched"})
        self.assertNotIn("failed", json.dumps(events))
        now[0] = 7
        apply("not_matched", "recovered")
        now[0] = 12
        apply("not_matched", "recovered-edit")
        self.assertEqual(len(session.calls), 0)
        status = monitor.managed_status("thread-a", "build")
        self.assertEqual(status["predicate"], {"pointer": "/status", "operator": "eq"})
        self.assertNotIn('"value"', json.dumps(status))

    def test_pending_event_replays_before_invalid_observation(self):
        now = [0.0]
        session = RecordingSession()
        monitor = Monitor(self.root, lambda _endpoint: session, clock=lambda: now[0])
        path = self.root / "build.json"
        watch = monitor.managed_create(
            "thread-a", "build", str(path), debounce_seconds=0,
            condition={"pointer": "/status", "operator": "eq", "value": "failed"},
        )
        supervisor = ManagedSupervisor(
            monitor, poll_interval=.01, clock=lambda: now[0], monotonic=lambda: now[0],
            sampler=lambda *_args: None,
        )
        worker = SimpleNamespace(watch_id=watch["id"], lifecycle_epoch=0)

        def apply(sample):
            row = monitor.managed_runtime_row(watch["id"])
            supervisor._apply_result(worker, row, sample, now[0])

        baseline = {"path": str(path), "state": "present", "sha256": "a",
                    "predicate": {"state": "not_matched"}}
        matched = {"path": str(path), "state": "present", "sha256": "b",
                   "predicate": {"state": "matched"}}
        apply(baseline)
        original_ingest = monitor.ingest
        fail_once = [True]

        def fail_delivery(binding, envelope):
            if fail_once[0]:
                fail_once[0] = False
                raise OSError("temporary ingest outage")
            return original_ingest(binding, envelope)

        monitor.ingest = fail_delivery
        apply(matched)
        self.assertIsNone(monitor.managed_status("thread-a", "build")["last_delivery"])
        apply({"path": str(path), "state": "unreadable", "error": "invalid_json"})
        status = monitor.managed_status("thread-a", "build")
        self.assertIsNotNone(status["last_delivery"])
        event = monitor.event(status["last_delivery"]["delivery_id"])
        self.assertEqual(event["envelope"]["data"]["current"], {"condition": "matched"})
        delivered_id = status["last_delivery"]["delivery_id"]
        apply({"path": str(path), "state": "unreadable", "error": "invalid_json"})
        self.assertEqual(delivered_id, monitor.managed_status("thread-a", "build")["last_delivery"]["delivery_id"])

    def test_live_old_receiver_fails_closed_but_offline_creation_stays_local(self):
        config = {"port": 8766}
        (self.root / "admin.token").write_text("admin-secret\n")
        with mock.patch.object(cli, "process_alive", return_value=False), \
                mock.patch.object(cli.urllib.request, "build_opener") as opener:
            cli.require_receiver_capability(self.root, config, "managed_json_predicates")
            opener.assert_not_called()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return json.dumps({"capabilities": {"managed_json_predicates": False}}).encode()

        fake_opener = mock.Mock()
        fake_opener.open.return_value = Response()
        with mock.patch.object(cli, "process_alive", return_value=True), \
                mock.patch.object(cli.urllib.request, "build_opener", return_value=fake_opener), \
                self.assertRaisesRegex(ValueError, "does not support"):
            cli.require_receiver_capability(self.root, config, "managed_json_predicates")


if __name__ == "__main__":
    unittest.main()
