import unittest
from unittest import mock

from codex_monitor.owner_health import OwnerHealthCache, probe_owner


class FakeRpc:
    def __init__(self, endpoint, *, timeout, token):
        self.endpoint = endpoint
        self.timeout = timeout
        self.token = token
        self.calls = []
        self.closed = False

    def notify(self, method, params):
        self.calls.append((method, params))

    def call(self, method, params, *, timeout):
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "fake"}
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "thread/loaded/list":
            if params.get("cursor") is None:
                return {"data": ["other"], "nextCursor": "page-2"}
            return {"data": [{"id": "thread-a"}]}
        if method == "thread/read":
            return {"thread": {"id": "thread-a", "status": {"type": "idle"}}}
        raise AssertionError(method)

    def close(self):
        self.closed = True


class OwnerHealthTest(unittest.TestCase):
    def test_explicit_owner_is_ready_to_receive_but_execution_remains_unverified(self):
        peers = []

        def factory(*args, **kwargs):
            peer = FakeRpc(*args, **kwargs)
            peers.append(peer)
            return peer

        result = probe_owner(
            "ws://127.0.0.1:8767", "thread-a", token="secret", rpc_factory=factory
        )
        self.assertEqual(result["status"], "ready-to-receive")
        self.assertTrue(result["transport_reachable"])
        self.assertTrue(result["thread_loaded"])
        self.assertEqual(result["account"]["status"], "present")
        self.assertEqual(result["account"]["credential_validation"], "unverified")
        self.assertEqual(result["model_execution"], "unverified")
        self.assertTrue(peers[0].closed)
        self.assertEqual(
            [method for method, _ in peers[0].calls],
            ["initialize", "initialized", "account/read", "thread/loaded/list", "thread/loaded/list", "thread/read"],
        )
        account = next(params for method, params in peers[0].calls if method == "account/read")
        self.assertEqual(account, {"refreshToken": False})
        thread_read = next(params for method, params in peers[0].calls if method == "thread/read")
        self.assertEqual(thread_read, {"threadId": "thread-a", "includeTurns": False})

    def test_shared_local_and_stdio_like_endpoints_are_unverified_without_factory(self):
        factory = mock.Mock(side_effect=AssertionError("must not create a peer"))
        for endpoint in ("shared-local", "local", "stdio://", "ssh://owner"):
            with self.subTest(endpoint=endpoint):
                result = probe_owner(endpoint, "thread-a", rpc_factory=factory)
                self.assertEqual(result["status"], "unverified")
                self.assertFalse(result["ready"])
        factory.assert_not_called()

    def test_auth_error_is_redacted_and_not_ready(self):
        class AuthPeer(FakeRpc):
            def call(self, method, params, *, timeout):
                if method == "account/read":
                    from codex_monitor.owner_health import _RpcError
                    raise _RpcError(401, "Bearer secret-token was rejected after logout")
                return super().call(method, params, timeout=timeout)

        result = probe_owner("wss://owner.example", "thread-a", rpc_factory=AuthPeer)
        self.assertEqual(result["status"], "auth-required")
        self.assertEqual(result["reason"], "authentication required")
        self.assertNotIn("secret-token", repr(result))

    def test_account_requires_auth_is_red_even_when_transport_is_reachable(self):
        class LoggedOutPeer(FakeRpc):
            def call(self, method, params, *, timeout):
                if method == "account/read":
                    self.calls.append((method, params))
                    return {"account": None, "requiresOpenaiAuth": True}
                return super().call(method, params, timeout=timeout)

        result = probe_owner(
            "ws://127.0.0.1:8767", "thread-a", rpc_factory=LoggedOutPeer
        )
        self.assertEqual(result["status"], "auth-required")
        self.assertEqual(result["account"]["status"], "auth-required")
        self.assertFalse(result["ready"])

    def test_custom_provider_without_account_is_not_claimed_as_logged_out(self):
        class CustomPeer(FakeRpc):
            def call(self, method, params, *, timeout):
                if method == "account/read":
                    self.calls.append((method, params))
                    return {"account": None, "requiresOpenaiAuth": False}
                return super().call(method, params, timeout=timeout)

        result = probe_owner(
            "ws://127.0.0.1:8767", "thread-a", rpc_factory=CustomPeer
        )
        self.assertEqual(result["status"], "ready-to-receive")
        self.assertEqual(result["account"]["status"], "absent")
        self.assertEqual(result["account"]["credential_validation"], "unverified")

    def test_rejected_endpoint_observation_does_not_echo_credentials(self):
        result = probe_owner("wss://user:secret-token@owner.example", "thread-a")
        self.assertEqual(result["status"], "unverified")
        self.assertNotIn("secret-token", repr(result))

    def test_unloaded_owner_does_not_read_thread_or_poll_a_model(self):
        class UnloadedPeer(FakeRpc):
            def call(self, method, params, *, timeout):
                if method == "thread/loaded/list":
                    self.calls.append((method, params))
                    return {"data": []}
                return super().call(method, params, timeout=timeout)

        peer = UnloadedPeer("ws://127.0.0.1:8767", timeout=.1, token=None)
        result = probe_owner("ws://127.0.0.1:8767", "thread-a", rpc_factory=lambda *a, **k: peer)
        self.assertEqual(result["status"], "unloaded")
        self.assertFalse(any(method == "thread/read" for method, _ in peer.calls))
        self.assertFalse(any(method in {"turn/start", "thread/resume", "thread/queue/add"} for method, _ in peer.calls))

    def test_thread_read_failure_is_not_hidden_by_loaded_list(self):
        class BrokenReadPeer(FakeRpc):
            def call(self, method, params, *, timeout):
                if method == "thread/read":
                    from codex_monitor.owner_health import _RpcError
                    raise _RpcError(-32000, "temporary native failure")
                return super().call(method, params, timeout=timeout)

        result = probe_owner("ws://127.0.0.1:8767", "thread-a", rpc_factory=BrokenReadPeer)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["ready"])

    def test_cache_is_ttl_and_size_bounded(self):
        calls = []
        clock = [0.0]

        def probe(endpoint, thread):
            calls.append((endpoint, thread))
            return {"status": "ready-to-receive", "endpoint": endpoint, "thread": thread}

        cache = OwnerHealthCache(probe=probe, ttl=2, max_entries=1, clock=lambda: clock[0])
        cache.get("ws://127.0.0.1:1", "a")
        cache.get("ws://127.0.0.1:1", "a")
        self.assertEqual(len(calls), 1)
        cache.get("ws://127.0.0.1:2", "b")
        cache.get("ws://127.0.0.1:1", "a")
        self.assertEqual(len(calls), 3)
        clock[0] = 3
        cache.get("ws://127.0.0.1:1", "a")
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
