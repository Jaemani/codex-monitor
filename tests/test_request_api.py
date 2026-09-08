import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from codex_monitor import cli
from codex_monitor.http import Server
from codex_monitor.monitor import MANAGED_SOURCE, Monitor


class RequestAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.json").write_text(json.dumps({
            "version": 1, "port": 8766, "sources": {}, "limits": {},
        }))
        self.monitor = Monitor(self.root, lambda _endpoint: self.fail("request tests must not wake Codex"))
        self.monitor.bind("work", "thread-user", "shared-local", ["build"])
        self.monitor.bind("other-work", "thread-user", "shared-local", ["other"])
        self.server = Server(
            self.monitor,
            {"build": "build-secret", "other": "other-secret"},
            "admin-secret",
            port=0,
        ).start(dispatch=False)
        self.addCleanup(self.server.close)

    def event(self, binding="work", source="build", event_id="event-1"):
        return self.monitor.ingest(binding, {
            "id": event_id,
            "source": source,
            "type": "build.failed",
            "data": {"summary": "untrusted event details"},
        })["delivery_id"]

    def cli_call(self, *args, thread="thread-user"):
        stdout, stderr = StringIO(), StringIO()
        environment = {"CODEX_THREAD_ID": thread} if thread is not None else {}
        with mock.patch.dict(os.environ, environment, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(["--state", str(self.root), *args])
        value = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
        return code, value, stderr.getvalue()

    def http_call(self, method, path, token, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.server.url + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    @staticmethod
    def unwrap(value):
        if isinstance(value, dict) and isinstance(value.get("request"), dict):
            return value["request"]
        return value

    @staticmethod
    def request_items(value):
        if isinstance(value, dict):
            for key in ("requests", "data", "items"):
                if isinstance(value.get(key), list):
                    return value[key]
        return value if isinstance(value, list) else []

    def test_cli_track_list_status_update_and_scope(self):
        delivery_id = self.event()
        code, tracked, error = self.cli_call(
            "request", "track", delivery_id, "--key", "build-status",
            "--summary", "Review the build", "--expires-in", "60",
            "--thread", "thread-user",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(tracked["original_delivery_id"], delivery_id)
        self.assertEqual(tracked["request_key"], "build-status")
        self.assertGreater(tracked["expires_at"], time.time())
        request_id = tracked["request_id"]

        code, listed, error = self.cli_call("request", "list", "--thread", "thread-user")
        self.assertEqual(code, 0, error)
        items = self.request_items(listed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["request_id"], request_id)

        code, status, error = self.cli_call("request", "status", request_id)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.unwrap(status)["request_key"], "build-status")
        revision = self.unwrap(status)["revision"]

        code, updated, error = self.cli_call(
            "request", "update", request_id, "--state", "in_progress",
            "--update-id", "update-1", "--revision", str(revision),
            "--message", "Started review",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(self.unwrap(updated)["state"], "in_progress")
        self.assertEqual(self.unwrap(updated)["revision"], revision + 1)

        code, _, error = self.cli_call(
            "request", "update", request_id, "--state", "done",
            "--update-id", "update-2", "--revision", str(revision),
        )
        self.assertEqual(code, 2)
        self.assertIn("revision", error)

        code, _, error = self.cli_call("request", "list", "--thread", "other-thread")
        self.assertEqual(code, 2)
        self.assertIn("CODEX_THREAD_ID", error)

    def test_cli_rejects_managed_delivery_and_thread_mismatch(self):
        watch = self.monitor.managed_create(
            "thread-user", "health", str(self.root / "health"), interval=.1,
        )
        managed_delivery = self.monitor.ingest(watch["binding"], {
            "id": "managed-event", "source": MANAGED_SOURCE,
            "type": "monitor.file.changed", "data": {},
        })["delivery_id"]
        code, _, error = self.cli_call(
            "request", "track", managed_delivery, "--key", "health",
        )
        self.assertEqual(code, 2)
        self.assertIn("managed", error.lower())

        delivery_id = self.event()
        code, _, error = self.cli_call(
            "request", "track", delivery_id, "--key", "wrong-thread",
            "--thread", "thread-other",
        )
        self.assertEqual(code, 2)
        self.assertIn("CODEX_THREAD_ID", error)

    def test_cli_and_http_pagination_and_query_allowlists(self):
        for index in range(3):
            delivery_id = self.event(event_id=f"page-event-{index}")
            code, _, error = self.cli_call(
                "request", "track", delivery_id, "--key", f"page-{index}",
            )
            self.assertEqual(code, 0, error)

        code, first, error = self.cli_call(
            "request", "list", "--thread", "thread-user", "--limit", "2",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(len(self.request_items(first)), 2)
        self.assertIsNotNone(first["next"])
        code, second, error = self.cli_call(
            "request", "list", "--thread", "thread-user", "--limit", "2",
            "--after", first["next"],
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(len(self.request_items(second)), 1)

        delivery_id = self.event(event_id="http-default-payload")
        code, tracked = self.http_call("POST", "/v1/requests", "build-secret", {
            "delivery_id": delivery_id, "request_key": "http-default-payload",
        })
        self.assertEqual(code, 201)
        request_id = self.unwrap(tracked)["request_id"]
        self.assertEqual(self.unwrap(tracked)["payload"], {})

        for path in (
            "/v1/requests?thread=thread-user&unknown=value",
            "/v1/requests?thread=thread-user&thread=other",
            f"/v1/requests/{request_id}?thread=thread-user",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.http_call("GET", path, "build-secret")[0], 400)
        self.assertEqual(
            self.http_call("GET", "/v1/requests?thread=thread-user&limit=0", "build-secret")[0],
            400,
        )
        self.assertEqual(
            self.http_call(
                "POST", "/v1/requests?unknown=value", "build-secret", {
                    "delivery_id": delivery_id, "request_key": "query-on-create",
                },
            )[0],
            400,
        )
        self.assertEqual(
            self.http_call(
                "POST", f"/v1/requests/{request_id}/updates?unknown=value", "build-secret", {
                    "update_id": "query-on-update", "state": "acknowledged", "expected_revision": 0,
                },
            )[0],
            400,
        )

    def test_http_track_list_status_update_is_source_and_thread_scoped(self):
        delivery_id = self.event()
        code, tracked = self.http_call(
            "POST", "/v1/requests", "build-secret", {
                "delivery_id": delivery_id,
                "request_key": "http-status",
                "payload": {"job": 17},
                "expires_at": time.time() + 120,
            },
        )
        self.assertIn(code, (200, 201))
        request_id = self.unwrap(tracked)["request_id"]

        code, listed = self.http_call(
            "GET", "/v1/requests?thread=thread-user", "build-secret",
        )
        self.assertEqual(code, 200)
        self.assertEqual(self.request_items(listed)[0]["request_id"], request_id)
        code, _ = self.http_call("GET", "/v1/requests", "build-secret")
        self.assertEqual(code, 400)
        code, other_list = self.http_call("GET", "/v1/requests?thread=other-thread", "build-secret")
        self.assertEqual(code, 200)
        self.assertEqual(self.request_items(other_list), [])

        code, status = self.http_call("GET", "/v1/requests/" + request_id, "build-secret")
        self.assertEqual(code, 200)
        revision = self.unwrap(status)["revision"]
        code, updated = self.http_call(
            "POST", "/v1/requests/" + request_id + "/updates", "build-secret", {
                "update_id": "http-update-1",
                "state": "completed",
                "expected_revision": revision,
                "detail": "Completed review",
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(self.unwrap(updated)["state"], "completed")

        self.assertEqual(self.http_call("POST", "/v1/requests", "admin-secret", {
            "delivery_id": delivery_id, "request_key": "forged", "payload": {},
        })[0], 401)
        self.assertEqual(self.http_call("GET", "/v1/requests?thread=thread-user", "admin-secret")[0], 401)
        self.assertIn(self.http_call("GET", "/v1/requests/" + request_id, "other-secret")[0], (403, 404))
        self.assertIn(self.http_call(
            "POST", "/v1/requests/" + request_id + "/updates", "other-secret", {
                "update_id": "forged-update", "state": "done", "expected_revision": revision + 1,
            },
        )[0], (403, 404))

    def test_request_pump_failure_does_not_skip_native_dispatch(self):
        native_calls = []

        def native_dispatch():
            native_calls.append(True)
            self.server.stop.set()
            self.monitor.wakeup.set()
            return False

        with mock.patch.object(
            self.server.request_dispatcher, "pump", side_effect=RuntimeError("request pump failed")
        ), mock.patch.object(self.monitor, "dispatch_once", side_effect=native_dispatch):
            self.server._dispatch()

        self.assertEqual(native_calls, [True])
        self.assertEqual(self.server.worker_error, "RuntimeError")

    def test_http_rejects_cross_source_track_origin_and_wrong_delivery(self):
        for invalid in (None, [], {}, True, "", "x" * 201):
            with self.subTest(delivery_id=invalid):
                self.assertEqual(self.http_call("POST", "/v1/requests", "build-secret", {
                    "delivery_id": invalid, "request_key": "invalid-receipt",
                })[0], 400)
        other_delivery = self.event("other-work", "other", "other-event")
        code, _ = self.http_call("POST", "/v1/requests", "build-secret", {
            "delivery_id": other_delivery, "request_key": "cross-source", "payload": {},
        })
        self.assertEqual(code, 404)

        delivery_id = self.event()
        code, _ = self.http_call(
            "POST", "/v1/requests", "build-secret", {
                "delivery_id": delivery_id, "request_key": "origin", "payload": {},
            }, headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(code, 403)
        code, _ = self.http_call("POST", "/v1/requests", "build-secret", {
            "delivery_id": delivery_id, "request_key": "bad-content", "payload": {},
        }, headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        code, _ = self.http_call("POST", "/v1/requests", "build-secret", {
            "delivery_id": delivery_id, "request_key": "too-large",
            "payload": {"data": "x" * 33000},
        })
        self.assertEqual(code, 413)


if __name__ == "__main__":
    unittest.main()
