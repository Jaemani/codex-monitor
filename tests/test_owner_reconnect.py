from dataclasses import replace
from pathlib import Path
import fcntl
import hashlib
import os
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from codex_monitor.dashboard import ReconnectAction
from codex_monitor.owner_reconnect import OwnerReconnect, ReconnectError


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.endpoint = "ws://127.0.0.1:8767"
        self.owner = self.root / "owner.plist"
        self.resident = self.root / "resident.plist"
        self.write(self.owner, {"Label": "test.owner", "ProgramArguments": ["/fake/codex", "app-server", "--listen", self.endpoint]})
        self.write(self.resident, {"Label": "test.resident", "ProgramArguments": ["/fake/codex-monitor", "resident", "--endpoint", self.endpoint, "--thread", "a", "--thread", "b"]})
        self.pid, self.restarts = 100, 0
        self.loaded = ["a", "b"]
        self.status = "idle"
        self.calls = []
        self.fresh_login_ok = True
        self.saved_threads = {"a", "b"}
        self.provider = "openai"
        self.providers_by_cwd = {}
        self.cwd_by_thread = {"a": "/project-a", "b": "/project-b"}
        self.platform = patch("codex_monitor.owner_reconnect.sys.platform", "darwin")
        self.platform.start(); self.addCleanup(self.platform.stop)
        self.cache = patch("codex_monitor.owner_reconnect.Path.home", return_value=self.root)
        self.cache.start(); self.addCleanup(self.cache.stop)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.backend = OwnerReconnect(self.root, jobs_dir=self.root, runner=self.runner, rpc_factory=self.rpc)

    def write(self, path, data):
        path.write_bytes(plistlib.dumps(data))
        path.chmod(0o600)

    def runner(self, args, **kwargs):
        if args[1] == "kickstart":
            self.restarts += 1
            self.pid += 1
            return subprocess.CompletedProcess(args, 0, "")
        path = self.owner if args[-1].endswith("test.owner") else self.resident
        spec = plistlib.loads(path.read_bytes())
        text = "path = " + str(path) + "\narguments = {\n" + "\n".join(spec["ProgramArguments"]) + "\n}\npid = " + str(self.pid)
        return subprocess.CompletedProcess(args, 0, text)

    def rpc(self, endpoint=None, **kwargs):
        test = self
        fresh = "command" in kwargs

        class Peer:
            def call(self, method, params, **kw):
                test.calls.append((fresh, method, params))
                if method == "thread/queue/list":
                    return {"data": []}
                if method == "thread/loaded/list":
                    return {"data": test.loaded}
                if method == "thread/read":
                    if fresh and params["threadId"] not in test.saved_threads:
                        return {"thread": None}
                    return {"thread": {"id": params["threadId"], "status": test.status,
                                       "modelProvider": test.provider, "cwd": test.cwd_by_thread.get(params["threadId"], "/other-project")}}
                if method == "config/read":
                    return {"config": {"model_providers": test.providers_by_cwd.get(params["cwd"], {})}}
                if method == "account/read":
                    return {"account": {"type": "chatgpt", "email": "current@example.com" if fresh or test.restarts else "old@example.com"}}
                if method == "account/rateLimits/read":
                    if not test.fresh_login_ok:
                        raise RuntimeError("private login failure")
                    return {"rateLimits": {}}
                raise AssertionError(method)

            def close(self):
                pass
        return Peer()

    def test_restart_uses_saved_login_and_preserves_exact_conversations(self):
        plan = self.backend.plan(self.endpoint, "a")
        self.assertEqual(self.restarts, 0)
        self.assertIn("2 conversations", plan.summary())
        result = self.backend.execute(plan)
        self.assertEqual(self.restarts, 1)
        self.assertIn("current login", result)
        self.assertIn("2 existing conversations restored", result)
        self.assertFalse(any(method in {"thread/resume", "thread/start", "turn/start", "account/login/start", "thread/queue/add"}
                             for _, method, _ in self.calls))
        self.assertFalse(any(params.get("refreshToken") for _, _, params in self.calls))

    def test_active_or_unknown_threads_block_restart(self):
        for state in ("active", "notLoaded", None):
            self.status = state
            with self.assertRaises(ReconnectError):
                self.backend.plan(self.endpoint, "a")
        self.assertEqual(self.restarts, 0)

    def test_uncovered_thread_blocks_restart(self):
        self.loaded.append("unmanaged")
        with self.assertRaisesRegex(ReconnectError, "cover every"):
            self.backend.plan(self.endpoint, "a")
        self.assertEqual(self.restarts, 0)

    def test_activity_or_configuration_change_after_preview_cancels_restart(self):
        plan = self.backend.plan(self.endpoint, "a")
        self.status = "active"
        with self.assertRaises(ReconnectError):
            self.backend.execute(plan)
        self.status = "idle"
        value = plistlib.loads(self.owner.read_bytes())
        value["KeepAlive"] = True
        self.write(self.owner, value)
        with self.assertRaisesRegex(ReconnectError, "changed"):
            self.backend.execute(plan)
        self.assertEqual(self.restarts, 0)

    def test_bad_current_login_or_missing_saved_thread_never_restarts_owner(self):
        plan = self.backend.plan(self.endpoint, "a")
        self.fresh_login_ok = False
        with self.assertRaises(RuntimeError):
            self.backend.execute(plan)
        self.fresh_login_ok = True
        self.saved_threads.remove("b")
        with self.assertRaisesRegex(ReconnectError, "missing"):
            self.backend.execute(plan)
        self.assertEqual(self.restarts, 0)

    def test_expired_plan_remote_or_unregistered_owner_are_rejected(self):
        plan = self.backend.plan(self.endpoint, "a")
        with self.assertRaisesRegex(ReconnectError, "expired"):
            self.backend.execute(replace(plan, created=plan.created - 61))
        for endpoint in ("shared-local", "wss://remote.example", "ws://127.0.0.1:9999"):
            with self.assertRaises(ReconnectError):
                self.backend.plan(endpoint, "a")
        self.assertEqual(self.restarts, 0)

    def test_custom_provider_blocks_restart_before_losing_live_configuration(self):
        plan = self.backend.plan(self.endpoint, "a")
        self.provider = "custom-provider"
        with self.assertRaisesRegex(ReconnectError, "model provider is absent"):
            self.backend.execute(plan)
        self.assertEqual(self.restarts, 0)

    def test_configured_custom_provider_is_preserved_without_requiring_a_named_provider(self):
        self.provider = "optional-gateway"
        definition = {"name": "Optional gateway", "base_url": "https://gateway.example/v1", "wire_api": "responses"}
        self.providers_by_cwd = {cwd: {self.provider: definition} for cwd in self.cwd_by_thread.values()}
        plan = self.backend.plan(self.endpoint, "a")
        self.assertIn("current login", self.backend.execute(plan))
        self.assertEqual(self.restarts, 1)
        configs = [params for _, method, params in self.calls if method == "config/read"]
        self.assertEqual(configs, [{"cwd": "/project-a", "includeLayers": False},
                                   {"cwd": "/project-b", "includeLayers": False}])
        self.assertFalse(any(method in {"config/value/write", "config/batchWrite", "thread/resume"}
                             for _, method, _ in self.calls))

    def test_custom_provider_from_one_project_does_not_validate_another(self):
        self.provider = "optional-gateway"
        self.providers_by_cwd = {"/project-a": {self.provider: {"name": "Gateway"}}}
        with self.assertRaisesRegex(ReconnectError, "model provider is absent"):
            self.backend.execute(self.backend.plan(self.endpoint, "a"))
        self.assertEqual(self.restarts, 0)

    def test_default_openai_needs_no_custom_provider_config(self):
        self.backend.execute(self.backend.plan(self.endpoint, "a"))
        self.assertFalse(any(method == "config/read" for _, method, _ in self.calls))

    def test_custom_provider_config_read_failure_is_redacted_and_blocks_restart(self):
        self.provider = "optional-gateway"
        original = self.rpc
        def factory(*args, **kwargs):
            peer = original(*args, **kwargs)
            call = peer.call
            def fail(method, params, **kw):
                if method == "config/read":
                    raise RuntimeError("private-config-secret")
                return call(method, params, **kw)
            peer.call = fail
            return peer
        self.backend.rpc_factory = factory
        with self.assertRaises(ReconnectError) as error:
            self.backend.execute(self.backend.plan(self.endpoint, "a"))
        self.assertNotIn("private-config-secret", str(error.exception))
        self.assertEqual(self.restarts, 0)

    def test_loaded_service_must_match_plist(self):
        self.backend.runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, "pid = 42\npath = /other\n")
        with self.assertRaisesRegex(ReconnectError, "identity differs"):
            self.backend.plan(self.endpoint, "a")

    def test_failed_restoration_never_triggers_a_second_restart(self):
        now = [0.0]
        self.backend.clock = lambda: now[0]
        self.backend.sleep = lambda seconds: now.__setitem__(0, now[0] + 10)
        original = self.rpc

        def rpc(*args, **kwargs):
            peer = original(*args, **kwargs)
            call = peer.call
            def missing(method, params, **kw):
                if method == "thread/loaded/list" and self.restarts:
                    return {"data": []}
                return call(method, params, **kw)
            peer.call = missing
            return peer

        self.backend.rpc_factory = rpc
        plan = self.backend.plan(self.endpoint, "a")
        with self.assertRaisesRegex(ReconnectError, "verification timed out"):
            self.backend.execute(plan)
        self.assertEqual(self.restarts, 1)

    def test_launchctl_timeout_after_acceptance_is_verified_without_retry(self):
        original = self.runner
        def runner(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == "kickstart":
                raise subprocess.TimeoutExpired(args, 5)
            return result
        self.backend.runner = runner
        plan = self.backend.plan(self.endpoint, "a")
        self.assertIn("current login", self.backend.execute(plan))
        self.assertEqual(self.restarts, 1)

    def test_concurrent_dashboard_cannot_restart_same_owner(self):
        plan = self.backend.plan(self.endpoint, "a")
        folder = self.root / ".cache/codex-monitor"
        folder.mkdir(parents=True)
        with (folder / (hashlib.sha256(self.endpoint.encode()).hexdigest() + ".reconnect.lock")).open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ReconnectError, "Another dashboard"):
                self.backend.execute(plan)
        self.assertEqual(self.restarts, 0)

    def test_account_mismatch_after_restart_is_not_reported_as_success(self):
        original = self.rpc
        def rpc(*args, **kwargs):
            peer = original(*args, **kwargs)
            call = peer.call
            def wrong(method, params, **kw):
                if method == "account/read" and self.restarts:
                    return {"account": {"type": "chatgpt", "email": "wrong@example.com"}}
                return call(method, params, **kw)
            peer.call = wrong
            return peer
        self.backend.rpc_factory = rpc
        plan = self.backend.plan(self.endpoint, "a")
        with self.assertRaisesRegex(ReconnectError, "account differs"):
            self.backend.execute(plan)
        self.assertEqual(self.restarts, 1)

    def test_second_explicit_action_is_required_and_selection_change_drops_preview(self):
        target = {"binding": "route", "thread": "a", "endpoint": self.endpoint}
        reader = Mock(root=self.root)
        controller = ReconnectAction(reader, backend=self.backend)
        # Synchronous thread driver makes the two explicit UI actions deterministic.
        def launch(*, target, **kwargs):
            return Mock(start=target)
        with patch("codex_monitor.dashboard.threading.Thread", side_effect=launch):
            controller.start(target)
            self.assertEqual(self.restarts, 0)
            self.assertIn("again", controller.notice(target))
            controller.start(dict(target, binding="other"))
            self.assertEqual(self.restarts, 0)
            self.assertIsNone(controller.notice(target))
            controller.start(dict(target, binding="other"))
            self.assertEqual(self.restarts, 1)


class PermissionReconnectTest(ReconnectTest):
    def policy_runner(self, args, **kwargs):
        if args[1:]==["resident", "--help"]:
            return subprocess.CompletedProcess(args, 0, "--sandbox --network-access")
        if args[1] in {"bootout", "bootstrap"}:
            if args[1] == "bootstrap":
                self.restarts += 1
                self.pid += 1
            return subprocess.CompletedProcess(args, 0, "")
        return self.runner(args, **kwargs)

    def test_policy_arguments_keep_unrelated_options(self):
        from codex_monitor.permission_reconnect import policy_args
        args = ("/bin/codex", "-c", 'model="example"', "-c", 'sandbox_mode="danger-full-access"',
                "app-server", "--listen", self.endpoint)
        result = policy_args(args, "read-only")
        self.assertIn('model="example"', result)
        self.assertIn('sandbox_mode="read-only"', result)
        self.assertNotIn('sandbox_mode="danger-full-access"', result)
        result = policy_args(("/bin/codex-monitor", "resident", "--sandbox", "workspace-write",
                              "--network-access", "--thread", "a"), "full", resident=True)
        self.assertNotIn("--network-access", result)
        self.assertIn("danger-full-access", result)

    def test_permission_preview_lists_shared_scope_and_requires_second_matching_action(self):
        self.backend.runner = self.policy_runner
        target = {"binding": "route", "thread": "a", "endpoint": self.endpoint}
        reader = Mock(root=self.root)
        controller = ReconnectAction(reader, backend=self.backend)
        with patch("codex_monitor.dashboard.threading.Thread", side_effect=lambda target, **kw: Mock(start=target)):
            controller.start(target, policy="full")
            self.assertIn("ALL 2", controller.notice(target))
            self.assertIn("not temporary", controller.notice(target))
            controller.start(target, policy="read-only")
            self.assertEqual(self.restarts, 0)
            self.assertEqual(controller.plan.policy, "read-only")

    def test_permission_success_persists_both_jobs_and_backups(self):
        self.backend.runner = self.policy_runner
        before = self.owner.read_bytes()
        plan = self.backend.plan(self.endpoint, "a", policy="read-only")
        with patch("codex_monitor.permission_reconnect._verify") as verify:
            message = self.backend.execute(plan)
        verify.assert_called_once()
        self.assertIn("read-only", message)
        self.assertIn('sandbox_mode="read-only"', plistlib.loads(self.owner.read_bytes())["ProgramArguments"])
        self.assertIn("read-only", plistlib.loads(self.resident.read_bytes())["ProgramArguments"])
        self.assertEqual(next((self.root/"permission-backups").glob("*/owner.plist")).read_bytes(), before)

    def test_failed_verification_restores_original_service_files(self):
        self.backend.runner = self.policy_runner
        originals = {p: p.read_bytes() for p in (self.owner,self.resident)}
        plan = self.backend.plan(self.endpoint, "a", policy="full")
        with patch("codex_monitor.permission_reconnect._verify", side_effect=ReconnectError("policy mismatch")):
            with self.assertRaisesRegex(ReconnectError,"Previous service configuration restored"):
                self.backend.execute(plan)
        for p, raw in originals.items():
            self.assertEqual(p.read_bytes(),raw)

    def test_queued_work_blocks_permission_change_before_mutation(self):
        original=self.backend.rpc_factory
        def rpc(*args,**kwargs):
            peer=original(*args,**kwargs); call=peer.call
            peer.call=lambda method,params,**kw: {"data":[{}]} if method=="thread/queue/list" else call(method,params,**kw)
            return peer
        self.backend.rpc_factory=rpc
        with self.assertRaisesRegex(ReconnectError,"empty queue"):
            self.backend.plan(self.endpoint,"a",policy="full")
        self.assertEqual(self.restarts,0)

    def test_effective_permission_verification_rejects_wrong_network(self):
        from codex_monitor.permission_reconnect import _verify
        plan = self.backend.plan(self.endpoint, "a", policy="read-only")
        self.restarts=1
        original=self.rpc
        def factory(*args,**kwargs):
            peer=original(*args,**kwargs);call=peer.call
            peer.call=lambda method,params,**kw: {"sandbox":{"type":"readOnly","networkAccess":True}} if method=="thread/resume" else call(method,params,**kw)
            return peer
        self.backend.rpc_factory=factory
        now=[0.0];self.backend.clock=lambda:now[0];self.backend.sleep=lambda seconds:now.__setitem__(0,now[0]+max(seconds,1))
        with self.assertRaisesRegex(ReconnectError,"permissions do not match"):
            _verify(self.backend,plan,(plan.owner,*plan.residents),{"type":"chatgpt","email":"current@example.com"})

    def test_effective_permission_verification_accepts_read_only_without_turn(self):
        from codex_monitor.permission_reconnect import _verify
        plan=self.backend.plan(self.endpoint,"a",policy="read-only");self.restarts=1
        original=self.rpc
        def factory(*args,**kwargs):
            peer=original(*args,**kwargs);call=peer.call
            peer.call=lambda method,params,**kw: {"sandbox":{"type":"readOnly","networkAccess":False}} if method=="thread/resume" else call(method,params,**kw)
            return peer
        self.backend.rpc_factory=factory
        _verify(self.backend,plan,(plan.owner,*plan.residents),{"type":"chatgpt","email":"current@example.com"})
        self.assertFalse(any(m in {"turn/start","thread/queue/add"} for _,m,_ in self.calls))
