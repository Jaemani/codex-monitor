from pathlib import Path
import os
import sys
import unittest
import json
import tempfile
import threading
import time
from unittest import mock

from codex_monitor.session import Rpc, AppServerSession, SharedLocalSession, SessionPool, server_token
from codex_monitor.errors import Uncertain, Retryable, Permanent


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.rpc = Rpc(command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py"))], timeout=.5)
        self.addCleanup(self.rpc.close)
        self.session = AppServerSession(self.rpc)

    def test_event_queues_behind_user_and_same_conversation_remains_usable(self):
        self.rpc.call("test/active", {"value": True})
        self.rpc.call("thread/queue/add", {"threadId": "thread-user", "clientUserMessageId": "human-1", "input": [{"type": "text", "text": "user work"}]})
        self.session.deliver("thread-user", "event-1", "webhook")
        queue = self.rpc.call("thread/queue/list", {"threadId": "thread-user"})["data"]
        self.assertEqual([x["clientUserMessageId"] for x in queue], ["human-1", "event-1"])
        self.rpc.call("test/active", {"value": False})
        self.session.deliver("thread-user", "event-2", "later webhook")
        items = self.rpc.call("thread/turns/list", {"threadId": "thread-user"})["data"][0]["items"]
        self.assertEqual([x["clientId"] for x in items], ["human-1", "event-1", "event-2"])
        methods = self.rpc.call("test/methods", {})
        self.assertFalse(set(methods) & {"thread/start", "thread/resume", "turn/start", "turn/interrupt"})

    def test_timeout_reconnects_and_reconciles_persisted_acceptance_once(self):
        original_rpc = Rpc
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "fake-state.json")
            instances = []

            def connect(*_args, **_kwargs):
                rpc = original_rpc(command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py")), state], timeout=.1)
                instances.append(rpc)
                return rpc

            with mock.patch("codex_monitor.session.Rpc", side_effect=connect):
                pool = SessionPool()
                try:
                    first = pool("shared-local")
                    first.rpc.call("test/drop-response", {})
                    with self.assertRaises(Uncertain):
                        first.deliver("thread-user", "lost-1", "once only")
                    self.assertTrue(first.rpc.closed)
                    self.assertIsNotNone(first.rpc.proc.returncode)
                    self.assertFalse(first.rpc.reader.is_alive())

                    second = pool("shared-local")
                    self.assertIsNot(first, second)
                    self.assertTrue(second.reconcile("thread-user", "lost-1"))
                    methods = second.rpc.call("test/methods", {})
                    self.assertEqual(methods.count("thread/queue/add"), 1)
                finally:
                    pool.close()

    def test_peer_exit_after_request_is_uncertain_and_child_is_reaped(self):
        with self.assertRaises(Uncertain):
            self.rpc.call("test/exit", {})
        self.rpc.close()
        self.assertIsNotNone(self.rpc.proc.returncode)
        self.assertFalse(self.rpc.reader.is_alive())

    def test_close_terminates_idle_writer_and_is_idempotent(self):
        rpc = Rpc(command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py"))], timeout=.5)
        rpc.close()
        rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)
        self.assertFalse(rpc.reader.is_alive())
        self.assertFalse(rpc.writer.is_alive())

    def test_stalled_stdin_reader_cannot_block_timeout_or_cleanup(self):
        rpc = Rpc(command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py"))], timeout=.3)
        rpc.call("test/stop-reading", {})
        barrier = threading.Barrier(41)
        errors = []

        def submit(index):
            barrier.wait()
            try:
                rpc.call("test/blocked", {"index": index, "payload": "x" * 32768})
            except (Retryable, Uncertain) as exc:
                errors.append(exc)

        workers = [threading.Thread(target=submit, args=(index,)) for index in range(40)]
        for worker in workers:
            worker.start()
        started = time.monotonic()
        barrier.wait()
        time.sleep(.05)
        self.assertGreater(rpc.outbound.qsize(), 0)
        for worker in workers:
            worker.join(timeout=2)
        rpc.close()

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(errors), len(workers))
        self.assertTrue(any(isinstance(error, Uncertain) for error in errors))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(rpc.proc.returncode)
        self.assertFalse(rpc.reader.is_alive())
        self.assertFalse(rpc.writer.is_alive())

    def test_closed_or_wrong_conversation_is_not_resumed_elsewhere(self):
        with self.assertRaises(Retryable):
            self.session.deliver("unloaded-desktop-thread", "event-1", "must not create a worker")
        methods = self.rpc.call("test/methods", {})
        self.assertNotIn("thread/queue/add", methods)
        self.assertNotIn("thread/resume", methods)

    def test_user_deleted_or_missing_message_is_ambiguous(self):
        self.assertIsNone(self.session.reconcile("thread-user", "not-present"))
        methods = self.rpc.call("test/methods", {})
        self.assertNotIn("thread/queue/add", methods)

    def test_inspect_distinguishes_queued_consumed_and_unknown_without_mutation(self):
        self.rpc.call("test/active", {"value": True})
        self.session.deliver("thread-user", "inspect-queued", "wait in queue")
        self.assertEqual(
            self.session.inspect("thread-user", "inspect-queued"),
            {
                "state": "queued",
                "submission_id": "queue-inspect-queued",
            },
        )

        self.rpc.call("test/active", {"value": False})
        self.assertEqual(
            self.session.inspect("thread-user", "inspect-queued"),
            {
                "state": "consumed",
                "submission_id": "message-inspect-queued",
            },
        )
        unknown = self.session.inspect("thread-user", "inspect-missing")
        self.assertEqual(unknown["state"], "unknown")
        self.assertIn("bounded history", unknown["reason"])

        methods = self.rpc.call("test/methods", {})
        inspected_methods = {
            "thread/loaded/list",
            "thread/queue/list",
            "thread/turns/list",
        }
        self.assertEqual(methods.count("thread/queue/add"), 1)
        self.assertFalse(set(methods) & {
            "thread/queue/delete",
            "thread/queue/start",
            "thread/start",
            "thread/resume",
            "turn/start",
            "turn/interrupt",
        })
        self.assertTrue(set(methods) & inspected_methods)

    def test_shared_local_queue_does_not_load_or_resume_the_target(self):
        session = SharedLocalSession(self.rpc)
        session.deliver("unloaded-desktop-thread", "external-1", "external event")
        self.assertTrue(session.reconcile("unloaded-desktop-thread", "external-1"))
        methods = self.rpc.call("test/methods", {})
        self.assertNotIn("thread/loaded/list", methods)
        self.assertFalse(set(methods) & {"thread/start", "thread/resume", "turn/start", "turn/interrupt"})
        self.assertEqual(methods.count("thread/queue/add"), 1)

    def test_shared_local_rejects_missing_and_archived_targets_without_queueing(self):
        session = SharedLocalSession(self.rpc)
        for thread in ("missing-thread", "archived-thread"):
            with self.subTest(thread=thread), self.assertRaises(Permanent):
                session.deliver(thread, "external-1", "external event")
        methods = self.rpc.call("test/methods", {})
        self.assertNotIn("thread/queue/add", methods)

    def test_submission_errors_preserve_acceptance_classification(self):
        self.rpc.call("test/fail-next", {"method": "thread/queue/add", "code": -32603, "message": "queue storage unavailable"})
        with self.assertRaises(Uncertain):
            self.session.deliver("thread-user", "unknown-1", "reconcile me")
        self.rpc.call("test/fail-next", {"method": "thread/queue/add", "code": -32001, "message": "server overloaded"})
        with self.assertRaises(Retryable):
            self.session.deliver("thread-user", "retry-1", "safe to retry")
        self.rpc.call("test/fail-next", {"method": "thread/queue/add", "code": -32600, "message": "invalid queued input"})
        with self.assertRaises(Permanent):
            self.session.deliver("thread-user", "bad-1", "reject me")

        shared = SharedLocalSession(self.rpc)
        self.rpc.call("test/fail-next", {"method": "thread/queue/list", "code": -32603, "message": "database busy"})
        with self.assertRaises(Retryable):
            shared.deliver("thread-user", "retry-2", "retry target check")

    def test_internal_error_after_persistence_reconciles_without_resubmission(self):
        self.rpc.call("test/error-after-add", {})
        with self.assertRaises(Uncertain):
            self.session.deliver("thread-user", "persisted-1", "accepted despite response error")
        self.assertTrue(self.session.reconcile("thread-user", "persisted-1"))
        methods = self.rpc.call("test/methods", {})
        self.assertEqual(methods.count("thread/queue/add"), 1)

    def test_incompatible_loaded_thread_api_is_permanent(self):
        self.rpc.call("test/fail-next", {"method": "thread/loaded/list", "code": -32601, "message": "Method not found"})
        with self.assertRaises(Permanent):
            self.session.deliver("thread-user", "unsupported-1", "do not retry forever")

    def test_reconciliation_exhausts_history_beyond_twenty_pages(self):
        self.session.deliver("thread-user", "old-accepted", "accepted before a long outage")
        self.rpc.call("test/history-prefix", {"count": 2100})
        self.assertTrue(self.session.reconcile("thread-user", "old-accepted"))
        methods = self.rpc.call("test/methods", {})
        self.assertGreater(methods.count("thread/turns/list"), 20)

    def test_reconciliation_stops_on_broken_repeating_cursor(self):
        self.rpc.call("test/cycle-cursor", {})
        with self.assertRaises(Retryable):
            self.session.reconcile("thread-user", "not-present")

    def test_reconciliation_has_a_hard_scan_budget(self):
        self.rpc.call("test/history-prefix", {"count": 1000})
        limited = AppServerSession(self.rpc, max_reconcile_pages=3, reconcile_timeout=30)
        with self.assertRaises(Uncertain):
            limited.reconcile("thread-user", "not-present")

    def test_reconciliation_has_an_elapsed_time_budget(self):
        clock = mock.Mock(side_effect=[10, 12])
        limited = AppServerSession(self.rpc, max_reconcile_pages=100, reconcile_timeout=1, clock=clock)
        with self.assertRaises(Uncertain):
            limited.reconcile("thread-user", "not-present")

    def test_remote_endpoints_reject_unsafe_transport_and_shell_syntax(self):
        for endpoint in ("ssh://host;touch-file", "ssh://-oProxyCommand=x", "ws://example.com:8765", "wss://user:secret@example.com"):
            with self.subTest(endpoint=endpoint), self.assertRaises(Permanent):
                Rpc(endpoint, timeout=.1)

    def test_server_token_file_is_private_absolute_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-token"
            path.write_text("file-secret-token\n")
            path.chmod(0o600)
            self.assertEqual(server_token({"CODEX_MONITOR_SERVER_TOKEN_FILE": str(path)}), "file-secret-token")
            self.assertEqual(server_token({"CODEX_MONITOR_SERVER_TOKEN": "env-secret-token",
                                           "CODEX_MONITOR_SERVER_TOKEN_FILE": "relative"}), "env-secret-token")

            with self.assertRaises(Permanent):
                server_token({"CODEX_MONITOR_SERVER_TOKEN_FILE": "relative-token"})
            with self.assertRaises(Permanent) as invalid:
                server_token({"CODEX_MONITOR_SERVER_TOKEN": "secret with spaces"})
            self.assertNotIn("secret with spaces", str(invalid.exception))

            if os.name == "posix":
                path.chmod(0o644)
                with self.assertRaises(Permanent):
                    server_token({"CODEX_MONITOR_SERVER_TOKEN_FILE": str(path)})

                fifo = Path(tmp) / "token-fifo"
                os.mkfifo(fifo, 0o600)
                started = time.monotonic()
                with self.assertRaises(Permanent):
                    server_token({"CODEX_MONITOR_SERVER_TOKEN_FILE": str(fifo)})
                self.assertLess(time.monotonic() - started, .5)

    def test_session_pool_passes_token_file_credential_to_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-token"
            path.write_text("service-file-token\n")
            path.chmod(0o600)
            fake_rpc = mock.Mock(closed=False)
            with mock.patch.dict(os.environ, {"CODEX_MONITOR_SERVER_TOKEN_FILE": str(path)}, clear=True), \
                 mock.patch("codex_monitor.session.Rpc", return_value=fake_rpc) as constructor:
                pool = SessionPool()
                pool("wss://server.example")
                pool.close()
            constructor.assert_called_once_with("wss://server.example", token="service-file-token")

    @unittest.skipIf(sys.platform == "win32", "Unix sockets are a POSIX transport")
    def test_custom_unix_endpoint_uses_websocket_handshake(self):
        from websockets.sync.server import unix_serve
        request_headers = []

        def peer(socket):
            request_headers.append(socket.request.headers)
            for raw in socket:
                request = json.loads(raw)
                if "id" in request:
                    result = {"userAgent": "contract-peer"} if request["method"] == "initialize" else {"data": ["thread-user"]}
                    socket.send(json.dumps({"id": request["id"], "result": result}))
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cm-ws-") as tmp:
            path = str(Path(tmp) / "server.sock")
            with unix_serve(peer, path) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    rpc = Rpc("unix://" + path, timeout=.5)
                    try:
                        self.assertEqual(rpc.call("thread/loaded/list", {}), {"data": ["thread-user"]})
                        self.assertNotIn("Sec-WebSocket-Extensions", request_headers[0])
                    finally: rpc.close()
                finally:
                    server.shutdown(); thread.join(timeout=2)
