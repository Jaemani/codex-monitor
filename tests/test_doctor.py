import unittest
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from codex_monitor import cli
from codex_monitor.doctor import probe_queue_target, unsupported_method_result
from codex_monitor.session import RpcError


class UnsupportedQueueRpc:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        raise RpcError({"code": -32601, "message": "method unavailable"})

    def close(self):
        pass


class WorkingQueueRpc:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"data": []}

    def close(self):
        pass


class DoctorTest(unittest.TestCase):
    def test_missing_queue_method_is_structured_and_bounded(self):
        rpc = UnsupportedQueueRpc()

        result = probe_queue_target(
            rpc,
            "thread-user",
            endpoint="shared-local",
            requested_surface="cli",
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["missing_method"], "thread/queue/list")
        self.assertEqual(result["error_code"], -32601)
        self.assertEqual(result["supported_baseline"]["version"], "0.153.4")
        self.assertIn("tested baseline", result["supported_baseline"]["claim"])
        self.assertIn("Upgrade", result["next_step"])
        self.assertTrue(result["read_only"])
        self.assertEqual(rpc.calls, [("thread/queue/list", {"threadId": "thread-user", "limit": 1})])

    def test_codex_0147_unknown_variant_prefix_preserves_actual_error_code(self):
        error = RpcError(
            {
                "code": -32600,
                "message": (
                    "Invalid request: unknown variant `thread/queue/list`, expected one of "
                    "`initialize`, `thread/start`, `thread/loaded/list`"
                ),
            }
        )

        result = unsupported_method_result(
            error,
            "thread/queue/list",
            endpoint="shared-local",
            requested_surface="cli",
        )

        self.assertEqual(result["error_code"], -32600)
        self.assertEqual(result["missing_method"], "thread/queue/list")

    def test_other_rpc_errors_are_left_to_existing_failure_handling(self):
        error = RpcError({"code": -32600, "message": "Invalid request: malformed params"})
        self.assertIsNone(
            unsupported_method_result(
                error,
                "thread/queue/list",
                endpoint="shared-local",
                requested_surface="cli",
            )
        )

        wrong_method = RpcError(
            {
                "code": -32600,
                "message": "Invalid request: unknown variant `thread/turns/list`, expected one of `initialize`",
            }
        )
        self.assertIsNone(
            unsupported_method_result(
                wrong_method,
                "thread/queue/list",
                endpoint="shared-local",
                requested_surface="cli",
            )
        )

    def test_cli_shared_local_doctor_reports_missing_queue_without_loaded_probe(self):
        rpc = UnsupportedQueueRpc()
        stdout = io.StringIO()
        with mock.patch.object(cli, "Rpc", return_value=rpc), mock.patch.object(
            cli, "server_token", return_value=None
        ), redirect_stdout(stdout):
            self.assertEqual(
                cli.main(
                    [
                        "doctor",
                        "--endpoint",
                        "shared-local",
                        "--thread",
                        "thread-user",
                    ]
                ),
                2,
            )
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["missing_method"], "thread/queue/list")
        self.assertEqual(result["supported_baseline"]["version"], "0.153.4")
        self.assertEqual(rpc.calls, [("thread/queue/list", {"threadId": "thread-user", "limit": 1})])

    def test_shared_local_success_keeps_consumer_readiness_unknown(self):
        rpc = WorkingQueueRpc()
        stdout = io.StringIO()
        with mock.patch.object(cli, "Rpc", return_value=rpc), mock.patch.object(
            cli, "server_token", return_value=None
        ), redirect_stdout(stdout):
            self.assertEqual(
                cli.main(
                    [
                        "doctor",
                        "--endpoint",
                        "shared-local",
                        "--thread",
                        "thread-user",
                    ]
                ),
                0,
            )
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ready"])
        self.assertTrue(result["queue_api"]["ready"])
        self.assertEqual(result["consumer_ready"], "unknown")
        self.assertIn("remains unverified", result["delivery_guarantee"])
        self.assertEqual(rpc.calls, [("thread/queue/list", {"threadId": "thread-user", "limit": 1})])


if __name__ == "__main__":
    unittest.main()
