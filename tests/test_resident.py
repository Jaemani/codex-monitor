import threading
import time
import unittest
import math

from codex_monitor.errors import Permanent, Retryable
from codex_monitor.resident import ResidentKeeper
from codex_monitor.session import RpcError


class FakeRpc:
    def __init__(self, loaded, *, resume_errors=None, queue_errors=None, loaded_errors=None):
        self.loaded = set(loaded)
        self.resume_errors = dict(resume_errors or {})
        self.queue_errors = dict(queue_errors or {})
        self.loaded_errors = list(loaded_errors or [])
        self.calls = []
        self.closed = False

    @staticmethod
    def _next_error(errors, thread):
        error = errors.get(thread)
        if isinstance(error, list):
            return error.pop(0) if error else None
        return error

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            if self.loaded_errors:
                error = self.loaded_errors.pop(0)
                if error is not None:
                    raise error
            return {"data": sorted(self.loaded)}
        if method == "thread/queue/list":
            error = self._next_error(self.queue_errors, params["threadId"])
            if error:
                raise error
            return {"data": []}
        if method == "thread/resume":
            error = self._next_error(self.resume_errors, params["threadId"])
            if error:
                raise error
            self.loaded.add(params["threadId"])
            return {"thread": {"id": params["threadId"]}}
        raise AssertionError(f"unexpected method: {method}")

    def close(self):
        self.closed = True


class ResidentTest(unittest.TestCase):
    def wait_for(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_rejects_shared_local_and_implicit_thread_sets(self):
        with self.assertRaisesRegex(ValueError, "shared-local"):
            ResidentKeeper("shared-local", ["thread-user"])
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", [])
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", "thread-user")
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", ["thread-user", "thread-user"])
        for endpoint in ("local", "ssh://owner"):
            with self.assertRaises(ValueError):
                ResidentKeeper(endpoint, ["thread-user"])
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                ResidentKeeper("unix:///owner.sock", ["thread-user"], health_interval=value)
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", ["thread-user"], health_interval=0)
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", ["thread-user"], backoff_initial=0)
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", ["thread-user"], backoff_max=0)
        with self.assertRaises(ValueError):
            ResidentKeeper("unix:///owner.sock", ["thread-user"], backoff_initial=2, backoff_max=1)

    def test_persistent_connection_resumes_saved_threads_and_only_probes_health(self):
        rpc = FakeRpc(["thread-a", "thread-b"])
        created = []

        def factory(endpoint):
            self.assertEqual(endpoint, "unix:///owner.sock")
            created.append(rpc)
            return rpc

        keeper = ResidentKeeper(
            "unix:///owner.sock",
            ["thread-a", "thread-b"],
            rpc_factory=factory,
            health_interval=.01,
        )
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: keeper.status()["connected"] and keeper.status()["subscribed"])

        snapshot = keeper.status()
        self.assertTrue(snapshot["threads"]["thread-a"]["ready"])
        self.assertTrue(snapshot["threads"]["thread-b"]["ready"])
        methods = [method for method, _ in rpc.calls]
        self.assertEqual(
            methods[:5],
            ["thread/loaded/list", "thread/queue/list", "thread/resume", "thread/queue/list", "thread/resume"],
        )
        self.assertTrue(all(
            params.get("excludeTurns") is True
            for method, params in rpc.calls
            if method == "thread/resume"
        ))
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        self.assertNotIn("thread/queue/add", methods)
        self.assertNotIn("turn/interrupt", methods)

        stop.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(created), 1)
        self.assertFalse(keeper.status()["connected"])

    def test_thread_resume_error_does_not_block_healthy_thread_or_repeat(self):
        rpc = FakeRpc(
            ["thread-a", "thread-b"],
            resume_errors={"thread-a": RpcError({"code": -32600, "message": "thread rejected"})},
        )
        keeper = ResidentKeeper("unix:///owner.sock", ["thread-a", "thread-b"], rpc_factory=lambda _: rpc, health_interval=.01)
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: keeper.status()["threads"]["thread-b"]["ready"])
        time.sleep(.03)
        status = keeper.status()
        stop.set()
        worker.join(timeout=1)

        self.assertFalse(status["threads"]["thread-a"]["subscribed"])
        self.assertEqual(status["threads"]["thread-a"]["error"]["code"], -32600)
        self.assertTrue(status["threads"]["thread-b"]["subscribed"])
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-a"],
            ["thread/resume"],
        )

    def test_thread_queue_capability_error_does_not_block_healthy_thread(self):
        rpc = FakeRpc(
            ["thread-a", "thread-b"],
            queue_errors={"thread-a": RpcError({"code": -32601, "message": "unsupported queue"})},
        )
        keeper = ResidentKeeper("unix:///owner.sock", ["thread-a", "thread-b"], rpc_factory=lambda _: rpc, health_interval=.01)
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: keeper.status()["threads"]["thread-b"]["ready"])
        status = keeper.status()
        stop.set()
        worker.join(timeout=1)

        self.assertEqual(status["threads"]["thread-a"]["error"]["code"], -32601)
        self.assertFalse(status["threads"]["thread-a"]["subscribed"])
        self.assertTrue(status["threads"]["thread-b"]["ready"])
        self.assertNotIn(
            ("thread/resume", {"threadId": "thread-a"}),
            rpc.calls,
        )

    def test_transient_queue_error_retries_on_same_owner_without_resuming_healthy_thread(self):
        rpc = FakeRpc(
            ["thread-a", "thread-b"],
            queue_errors={
                "thread-a": [
                    RpcError({"code": -32000, "message": "temporary queue database busy"}),
                    None,
                ]
            },
        )
        created = []
        keeper = ResidentKeeper(
            "unix:///owner.sock",
            ["thread-a", "thread-b"],
            rpc_factory=lambda _: (created.append(rpc) or rpc),
            health_interval=.01,
            backoff_initial=.001,
            backoff_max=.002,
        )
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: keeper.status()["threads"]["thread-a"]["ready"])
        stop.set()
        worker.join(timeout=1)

        self.assertEqual(len(created), 1)
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/queue/list" and params["threadId"] == "thread-a"],
            ["thread/queue/list", "thread/queue/list"],
        )
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-a"],
            ["thread/resume"],
        )
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-b"],
            ["thread/resume"],
        )

    def test_transient_resume_error_retries_target_without_duplicate_healthy_resume(self):
        rpc = FakeRpc(
            ["thread-a", "thread-b"],
            resume_errors={
                "thread-a": [
                    RpcError({"code": -32000, "message": "temporary owner busy"}),
                    None,
                ]
            },
        )
        created = []
        keeper = ResidentKeeper(
            "unix:///owner.sock",
            ["thread-a", "thread-b"],
            rpc_factory=lambda _: (created.append(rpc) or rpc),
            health_interval=.01,
            backoff_initial=.001,
            backoff_max=.002,
        )
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: keeper.status()["threads"]["thread-a"]["ready"])
        stop.set()
        worker.join(timeout=1)

        self.assertEqual(len(created), 1)
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-a"],
            ["thread/resume", "thread/resume"],
        )
        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-b"],
            ["thread/resume"],
        )

    def test_missing_and_writer_conflict_are_stable_registration_errors(self):
        cases = (
            RpcError({"code": -32000, "message": "thread not found: thread-a"}),
            RpcError({"code": -32603, "message": "thread already has an active writer"}),
        )
        for error in cases:
            with self.subTest(message=str(error)):
                rpc = FakeRpc(["thread-a"], resume_errors={"thread-a": error})
                keeper = ResidentKeeper(
                    "unix:///owner.sock",
                    ["thread-a"],
                    rpc_factory=lambda _: rpc,
                    health_interval=.01,
                    backoff_initial=.001,
                    backoff_max=.002,
                )
                stop = threading.Event()
                worker = threading.Thread(target=keeper.run, args=(stop,))
                worker.start()
                self.wait_for(lambda: keeper.status()["threads"]["thread-a"]["error"] is not None)
                time.sleep(.03)
                stop.set()
                worker.join(timeout=1)
                self.assertEqual(
                    len([1 for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-a"]),
                    1,
                )

    def test_health_probe_error_clears_ready_without_reresuming(self):
        probe_failure = threading.Event()

        class ControlledRpc(FakeRpc):
            def call(self, method, params):
                if method == "thread/loaded/list" and probe_failure.is_set():
                    self.calls.append((method, params))
                    raise RpcError({"code": -32000, "message": "temporary probe failure"})
                return super().call(method, params)

        rpc = ControlledRpc(["thread-a"])
        keeper = ResidentKeeper("unix:///owner.sock", ["thread-a"], rpc_factory=lambda _: rpc, health_interval=.01)
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        try:
            self.wait_for(lambda: keeper.status()["threads"]["thread-a"]["ready"])
            # Hold the failure until observed instead of sampling a 10 ms window.
            probe_failure.set()
            self.wait_for(lambda: not keeper.status()["threads"]["thread-a"]["ready"])
            self.assertTrue(keeper.status()["threads"]["thread-a"]["subscribed"])
            probe_failure.clear()
            self.wait_for(lambda: keeper.status()["threads"]["thread-a"]["ready"])
        finally:
            stop.set()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

        self.assertEqual(
            [method for method, params in rpc.calls if method == "thread/resume" and params["threadId"] == "thread-a"],
            ["thread/resume"],
        )

    def test_transport_loss_reconnects_with_resume_for_every_thread(self):
        first = FakeRpc(["thread-a", "thread-b"], loaded_errors=[None, Retryable("connection lost")])
        second = FakeRpc([])
        instances = iter([first, second])
        created = []

        def factory(_):
            rpc = next(instances)
            created.append(rpc)
            return rpc

        keeper = ResidentKeeper(
            "unix:///owner.sock",
            ["thread-a", "thread-b"],
            rpc_factory=factory,
            health_interval=.01,
            backoff_initial=.001,
            backoff_max=.002,
        )
        stop = threading.Event()
        worker = threading.Thread(target=keeper.run, args=(stop,))
        worker.start()
        self.wait_for(lambda: len(created) == 2 and keeper.status()["threads"]["thread-a"]["ready"])

        self.assertEqual(
            [method for method, _ in first.calls],
            [
                "thread/loaded/list",
                "thread/queue/list",
                "thread/resume",
                "thread/queue/list",
                "thread/resume",
                "thread/loaded/list",
            ],
        )
        self.assertEqual(
            [method for method, _ in second.calls[:5]],
            ["thread/loaded/list", "thread/queue/list", "thread/resume", "thread/queue/list", "thread/resume"],
        )
        stop.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_status_reports_approval_gap_without_autoapproval(self):
        status = ResidentKeeper("unix:///owner.sock", ["thread-user"]).status()
        self.assertEqual(status["protocol"]["approval_policy"], "no_autoanswer")
        self.assertEqual(status["protocol"]["server_requests"], "awaiting_native_interactive_handling")
        self.assertIn("sends no response", status["protocol"]["integration_needed"])

    def test_permanent_endpoint_error_does_not_retry(self):
        attempts = []

        def factory(_):
            attempts.append(True)
            raise Permanent("invalid owner endpoint")

        keeper = ResidentKeeper("unix:///owner.sock", ["thread-user"], rpc_factory=factory)
        with self.assertRaisesRegex(Permanent, "invalid owner endpoint"):
            keeper.run(threading.Event())
        status = keeper.status()
        self.assertEqual(len(attempts), 1)
        self.assertFalse(status["connected"])
        self.assertIn("invalid owner endpoint", status["last_error"]["message"])

    def test_successful_health_probe_resets_backoff_before_later_disconnect(self):
        waits = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, delay):
                waits.append(delay)
                return self.stopped

        stop = Stop()
        rpc = FakeRpc([], loaded_errors=[None, None, Retryable("later outage")])
        attempts = []

        def factory(_):
            attempts.append(True)
            if len(attempts) == 1:
                raise Retryable("initial outage")
            if len(attempts) == 3:
                stop.stopped = True
                return FakeRpc([])
            return rpc

        keeper = ResidentKeeper("unix:///owner.sock", ["thread-user"], rpc_factory=factory,
                                health_interval=10, backoff_initial=1, backoff_max=8)
        keeper.run(stop)
        self.assertEqual(waits[:4], [1, 10, 10, 1])

    def test_stop_during_registration_prevents_following_resume(self):
        stop = threading.Event()
        rpc = FakeRpc([])
        original = rpc.call

        def call(method, params):
            result = original(method, params)
            if method == "thread/queue/list":
                stop.set()
            return result

        rpc.call = call
        keeper = ResidentKeeper("unix:///owner.sock", ["thread-a", "thread-b"], rpc_factory=lambda _: rpc)
        keeper.run(stop)
        self.assertFalse(any(method == "thread/resume" for method, _ in rpc.calls))
        self.assertTrue(rpc.closed)


if __name__ == "__main__":
    unittest.main()
