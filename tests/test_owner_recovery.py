import threading
import unittest
from unittest import mock

from codex_monitor.dashboard import AuthRetry, DashboardError
from codex_monitor.owner_health import _RpcError
from codex_monitor.owner_recovery import retry_owner_auth


class RecoveryPeer:
    def __init__(self):
        self.calls = []
        self.closed = False

    def call(self, method, params, *, timeout):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "account/rateLimits/read":
            return {"rateLimits": {"primary": {"usedPercent": 90}}}
        raise AssertionError(method)

    def notify(self, *args):
        pass

    def close(self):
        self.closed = True


class OwnerRecoveryTest(unittest.TestCase):
    def test_one_refresh_and_verification_without_replaying_or_restarting(self):
        peer = RecoveryPeer()
        result = retry_owner_auth("unix:///tmp/test-owner", rpc_factory=lambda *a, **k: peer)
        self.assertIn("Account access verified", result)
        self.assertIn("no work was replayed", result)
        self.assertEqual(peer.calls, [
            ("initialize", {"clientInfo": {"name": "codex-monitor-auth-retry", "version": "0.1.0"},
                            "capabilities": {"experimentalApi": True}}),
            ("account/read", {"refreshToken": False}),
            ("account/read", {"refreshToken": True}),
            ("account/rateLimits/read", {}),
        ])
        self.assertTrue(peer.closed)

    def test_refresh_presence_does_not_hide_failed_account_access(self):
        peer = RecoveryPeer()
        original = peer.call

        def call(method, params, *, timeout):
            if method == "account/rateLimits/read":
                raise _RpcError(401, "Bearer TOP_SECRET refresh token rejected")
            return original(method, params, timeout=timeout)

        peer.call = call
        result = retry_owner_auth("unix:///tmp/test-owner", rpc_factory=lambda *a, **k: peer)
        self.assertIn("Authentication still failed", result)
        self.assertNotIn("TOP_SECRET", result)
        self.assertTrue(peer.closed)

    def test_missing_or_external_login_does_not_refresh(self):
        for account in (None, {"type": "apiKey"}, {"type": "unknown"}):
            peer = RecoveryPeer()
            original = peer.call

            def call(method, params, *, timeout):
                if method == "account/read":
                    self.assertFalse(params["refreshToken"])
                    return {"account": account}
                return original(method, params, timeout=timeout)

            peer.call = call
            result = retry_owner_auth("unix:///tmp/test-owner", rpc_factory=lambda *a, **k: peer)
            self.assertNotIn("verified", result)
            self.assertTrue(peer.closed)

    def test_shared_local_or_unsafe_endpoint_does_not_connect(self):
        factory = mock.Mock(side_effect=AssertionError("unexpected connect"))
        for endpoint in ("shared-local", "ws://example.com", "wss://user:secret@example.com"):
            self.assertIn("explicit owner", retry_owner_auth(endpoint, rpc_factory=factory))
        factory.assert_not_called()

    def test_duplicate_clicks_are_bounded_and_results_stay_with_selected_route(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        reader = mock.Mock()
        reader.selected_binding.return_value = {"endpoint": "unix:///tmp/test-owner"}
        calls = []

        def retry(endpoint, *, token):
            calls.append(endpoint)
            entered.set()
            release.wait(2)
            finished.set()
            return "Account access verified."

        controller = AuthRetry(reader, retry=retry, clock=lambda: 1.0)
        target = {"binding": "route", "thread": "thread-a", "endpoint": "unix:///tmp/test-owner"}
        with mock.patch("codex_monitor.dashboard.server_token", return_value=None):
            self.assertIsNone(controller.start(target))
            self.assertTrue(entered.wait(1))
            self.assertIn("already running", controller.start(target))
            self.assertIsNone(controller.notice(dict(target, thread="thread-b")))
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(calls, [target["endpoint"]])
        # Whether the worker is publishing or finished, a second click is rejected.
        self.assertIsNotNone(controller.start(target))

    def test_changed_binding_is_rejected_before_any_refresh(self):
        reader, retry = mock.Mock(), mock.Mock()
        reader.selected_binding.side_effect = DashboardError("selected binding identity changed")
        controller = AuthRetry(reader, retry=retry)
        with self.assertRaises(DashboardError):
            controller.start({"binding": "route", "thread": "thread-a", "endpoint": "unix:///tmp/old"})
        retry.assert_not_called()
        self.assertFalse(controller.running)
