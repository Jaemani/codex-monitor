#!/usr/bin/env python3
"""Bounded black-box comparison of the Python and Rust runtimes.

Uses disposable state and an unavailable owner endpoint. It verifies persisted
receiver and file-monitor behavior without starting a model or touching a real
conversation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

INTERVAL = 0.25
DEBOUNCE = 0.75
TIMEOUT = 15.0
SETTLE = 1.25


class CheckFailure(RuntimeError):
    pass


def run_command(executable, state, *args, env=None, check=True):
    process = subprocess.run(
        [*executable, "--state", str(state), *args], env=env, text=True,
        capture_output=True, timeout=15,
    )
    result = {"returncode": process.returncode, "stdout": process.stdout.strip(),
              "stderr": process.stderr.strip()}
    if check and process.returncode:
        raise CheckFailure(
            f"command failed ({process.returncode}): {' '.join(args)}: {process.stderr.strip()}"
        )
    if process.returncode == 0 and process.stdout.strip():
        try:
            result["json"] = json.loads(process.stdout)
        except json.JSONDecodeError:
            result["json"] = {"stdout": process.stdout.strip()}
    return result


def request(port, token, path, payload=None, origin=None):
    data = None if payload is None else json.dumps(payload).encode()
    value = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if origin:
        value.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(value, timeout=3) as response:
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            body = json.loads(raw)
        except Exception:
            body = raw.decode(errors="replace")
        return {"status": error.code, "body": body}


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def file_sample(path):
    return {"path": str(path), "state": "present",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class RuntimeHarness:
    def __init__(self, kind, binary, parent):
        self.kind = kind
        self.state = pathlib.Path(tempfile.mkdtemp(prefix=f"functional-{kind}-", dir=parent))
        self.port = free_port()
        self.executable = [str(binary)] if kind == "rust" else [str(binary), "-m", "codex_monitor"]
        self.environment = os.environ.copy()
        variable = "CODEX_MONITOR_RUST_HOME" if kind == "rust" else "CODEX_MONITOR_HOME"
        self.environment[variable] = str(self.state)
        self.process = None
        self.checks = []

    @property
    def database(self):
        return self.state / ("rust.sqlite3" if self.kind == "rust" else "monitor.sqlite3")

    def check(self, name, passed, expected=None, actual=None):
        item = {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}
        self.checks.append(item)
        if not passed:
            raise CheckFailure(f"{self.kind}: {name}: expected {expected!r}, observed {actual!r}")

    def cli(self, *args, check=True, thread=None):
        environment = self.environment.copy()
        if thread:
            environment["CODEX_THREAD_ID"] = thread
        return run_command(self.executable, self.state, *args, env=environment, check=check)

    def wait(self, label, probe, timeout=TIMEOUT):
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            last = probe()
            if last is not None and last is not False:
                return last
            time.sleep(0.1)
        raise CheckFailure(f"{self.kind}: timed out waiting for {label}; last={last!r}")

    def status_if_ready(self):
        if self.process is not None and self.process.poll() is not None:
            raise CheckFailure(f"{self.kind}: receiver exited during startup")
        try:
            value = request(self.port, self.admin, "/v1/status")
            return value if value["status"] == 200 else None
        except OSError:
            return None

    def start(self):
        self.process = subprocess.Popen(
            [*self.executable, "--state", str(self.state), "serve"], env=self.environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.wait("receiver readiness", self.status_if_ready, timeout=10)

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def initialize(self):
        self.cli("init", "--port", str(self.port))
        self.cli("source", "test")
        self.cli("bind", "route", "--thread", "thread-http", "--source", "test",
                 "--endpoint", "ws://127.0.0.1:1")
        self.admin = (self.state / "admin.token").read_text().strip()
        self.source = (self.state / "source-test.token").read_text().strip()
        self.start()

    def watch_action_args(self, action, name, thread):
        args = ["monitor", action, name]
        if self.kind == "rust":
            args += ["--thread", thread]
        return args

    def create_watch(self, name, thread, path, debounce=0, predicate=False):
        args = ["monitor", "create", name, "--file", str(path), "--thread", thread,
                "--interval", str(INTERVAL), "--endpoint", "ws://127.0.0.1:1",
                "--debounce", str(debounce)]
        if predicate:
            args += ["--json-pointer", "/value", "--operator", "eq", "--value", "1"]
        created = self.cli(*args, thread=thread)["json"]
        self.check(f"{name}: exact conversation", created.get("thread") == thread,
                   thread, created.get("thread"))
        return created

    def watch_status(self, name, thread):
        return self.cli(*self.watch_action_args("status", name, thread), thread=thread)["json"]

    def events(self, binding):
        with sqlite3.connect(self.database) as database:
            rows = database.execute(
                "SELECT seq,id,binding,envelope,state,attempts,error FROM events "
                "WHERE binding=? ORDER BY seq", (binding,),
            ).fetchall()
        return [{"seq": row[0], "id": row[1], "binding": row[2],
                 "envelope": json.loads(row[3]), "state": row[4],
                 "attempts": row[5], "error": row[6]} for row in rows]

    def wait_baseline(self, name, thread, expected):
        status = self.wait(
            f"{name} baseline",
            lambda: value if (value := self.watch_status(name, thread)).get("last_sample") == expected else None,
        )
        count = len(self.events(status["binding"]))
        self.check(f"{name}: silent persisted baseline", count == 0, 0, count)
        return status

    def wait_event(self, binding, count, current):
        records = self.wait(
            f"{binding} event {count}",
            lambda: values if len(values := self.events(binding)) >= count else None,
        )
        self.check(f"{binding}: exact event count {count}", len(records) == count, count, len(records))
        observed = records[-1]["envelope"].get("data", {}).get("current")
        self.check(f"{binding}: transition {count}", observed == current, current, observed)
        return records[-1]

    def stable(self, binding, count, label, duration=SETTLE):
        deadline, observed = time.monotonic() + duration, None
        while time.monotonic() < deadline:
            observed = len(self.events(binding))
            if observed != count:
                break
            time.sleep(0.1)
        self.check(label, observed == count, count, observed)

    def watch_action(self, action, name, thread):
        return self.cli(*self.watch_action_args(action, name, thread), thread=thread)["json"]

    def invalid_observed(self, name, thread, before):
        if self.kind == "python":
            value = self.wait(
                f"{name} invalid JSON sample",
                lambda: status if (status := self.watch_status(name, thread)).get("last_sample_error") else None,
            )
            return value["last_sample_error"]
        return self.wait(
            f"{name} invalid JSON sample",
            lambda: count if (count := request(self.port, self.admin, "/v1/status")["body"]
                              .get("collector", {}).get("sample_errors", 0)) > before else None,
        )

    def predicate_probe(self):
        thread = "thread-predicate"
        path = self.state / "predicate.json"
        path.write_text('{"value":0,"noise":0}\n')
        watch = self.create_watch("predicate", thread, path, DEBOUNCE, True)
        binding = watch["binding"]
        unmatched, matched = {"condition": "not_matched"}, {"condition": "matched"}
        self.wait_baseline("predicate", thread, unmatched)
        self.stable(binding, 0, "predicate: unchanged baseline stays silent")

        path.write_text('{"value":1,"noise":0}\n')
        self.wait_event(binding, 1, matched)
        self.stable(binding, 1, "predicate: stable match emits once")
        path.write_text('{"value":1,"noise":1}\n')
        self.stable(binding, 1, "predicate: unrelated JSON change stays silent")

        before = 0
        if self.kind == "rust":
            before = request(self.port, self.admin, "/v1/status")["body"]["collector"]["sample_errors"]
        path.write_text("{invalid\n")
        invalid = self.invalid_observed("predicate", thread, before)
        self.stable(binding, 1, "predicate: invalid JSON emits no transition")
        path.write_text('{"value":0,"noise":2}\n')
        self.wait_event(binding, 2, unmatched)
        self.stable(binding, 2, "predicate: valid JSON recovery is stable")

        paused = self.watch_action("pause", "predicate", thread)
        self.check("predicate: pause persisted", paused.get("enabled") is False, False, paused)
        path.write_text('{"value":1,"noise":3}\n')
        self.stable(binding, 2, "predicate: paused mutation emits nothing", duration=2.5)
        resumed = self.watch_action("resume", "predicate", thread)
        self.check("predicate: resume persisted", resumed.get("enabled") is True, True, resumed)
        self.wait_event(binding, 3, matched)
        self.stable(binding, 3, "predicate: resumed transition emits once")

        self.stop()
        path.write_text('{"value":0,"noise":4}\n')
        restarted_at = time.monotonic()
        self.start()
        self.wait_event(binding, 4, unmatched)
        elapsed = time.monotonic() - restarted_at
        self.check("predicate: restart re-observes debounce window",
                   elapsed >= DEBOUNCE * 0.75, f">={DEBOUNCE * 0.75:.3f}s", elapsed)
        self.stable(binding, 4, "predicate: restarted transition emits once")
        envelope_contract, top_level_keys = [], []
        expected_pairs = [(unmatched, matched), (matched, unmatched),
                          (unmatched, matched), (matched, unmatched)]
        for index, (record, (previous, current)) in enumerate(
                zip(self.events(binding), expected_pairs), 1):
            envelope = record["envelope"]
            data = envelope.get("data") or {}
            observed = {
                "source": envelope.get("source"),
                "type": envelope.get("type"),
                "data_keys": sorted(data),
                "watch": data.get("watch"),
                "path": "$WATCH" if data.get("path") == str(path) else data.get("path"),
                "previous": data.get("previous"),
                "current": data.get("current"),
            }
            expected = {
                "source": "managed/file", "type": "monitor.file.changed",
                "data_keys": ["current", "path", "previous", "watch"],
                "watch": "predicate", "path": "$WATCH",
                "previous": previous, "current": current,
            }
            self.check(f"predicate: event {index} envelope contract",
                       observed == expected, expected, observed)
            envelope_contract.append(observed)
            top_level_keys.append(sorted(envelope))
        return {"events": 4, "transitions": ["matched", "not_matched", "matched", "not_matched"],
                "invalid_observation": invalid, "restart_transition_seconds": elapsed,
                "envelope_contract": envelope_contract, "top_level_keys": top_level_keys}

    def path_policy_probe(self):
        target = self.state / "symlink-target.json"
        target.write_text('{"kind":"target"}\n')
        link = self.state / "symlink.json"
        link.symlink_to(target)
        watch = self.create_watch("symlink", "thread-path", link)
        status = self.wait(
            "symlink unreadable baseline",
            lambda: value if (value := self.watch_status("symlink", "thread-path"))
            .get("last_sample") and value["last_sample"].get("state") == "unreadable" else None,
        )
        baseline = status["last_sample"]
        self.check("symlink: collector does not follow configured link",
                   baseline.get("state") == "unreadable", "unreadable", baseline)
        self.check("symlink: baseline is silent", len(self.events(watch["binding"])) == 0,
                   0, len(self.events(watch["binding"])))
        link.unlink()
        link.write_text('{"kind":"regular"}\n')
        self.wait_event(watch["binding"], 1, file_sample(link))
        self.stable(watch["binding"], 1, "symlink: regular-file recovery emits once")

        fifo = self.state / "managed.fifo"
        os.mkfifo(fifo)
        fifo_watch = self.create_watch("fifo", "thread-path", fifo)
        fifo_baseline = {"path": str(fifo), "state": "unreadable", "error": "not_regular_file"}
        self.wait_baseline("fifo", "thread-path", fifo_baseline)
        fifo.unlink()
        fifo.write_text("regular after fifo\n")
        self.wait_event(fifo_watch["binding"], 1, file_sample(fifo))
        self.stable(fifo_watch["binding"], 1, "fifo: regular-file recovery emits once")
        return {
            "symlink": {"configuration_accepted": True, "baseline": baseline,
                        "regular_file_recovery": True},
            "fifo": {"configuration_accepted": True, "baseline": fifo_baseline,
                     "regular_file_recovery": True},
        }

    def isolation_probe(self):
        paths = {"thread-a": self.state / "isolation-a.txt",
                 "thread-b": self.state / "isolation-b.txt"}
        watches = {}
        for thread, path in paths.items():
            path.write_text(f"baseline {thread}\n")
            watches[thread] = self.create_watch("isolation", thread, path)
        for thread, path in paths.items():
            self.wait_baseline("isolation", thread, file_sample(path))
        binding_a, binding_b = watches["thread-a"]["binding"], watches["thread-b"]["binding"]
        paths["thread-a"].write_text("changed a\n")
        self.wait_event(binding_a, 1, file_sample(paths["thread-a"]))
        self.stable(binding_a, 1, "isolation: thread A emits once")
        self.stable(binding_b, 0, "isolation: thread A change does not emit for B")
        paths["thread-b"].write_text("changed b\n")
        self.wait_event(binding_b, 1, file_sample(paths["thread-b"]))
        self.stable(binding_a, 1, "isolation: thread B change does not emit for A")
        self.stable(binding_b, 1, "isolation: thread B emits once")
        return {"thread-a": 1, "thread-b": 1, "cross_delivery": False}

    def http_probe(self):
        event = {"id": "stable-event", "source": "test", "type": "test.changed",
                 "data": {"message": "hello"}, "hops": 0}
        accept = request(self.port, self.source, "/v1/events/route", event)
        duplicate = request(self.port, self.source, "/v1/events/route", event)
        conflict = request(self.port, self.source, "/v1/events/route",
                           {**event, "data": {"message": "changed"}})
        bad_auth = request(self.port, "wrong", "/v1/events/route", event)
        bad_origin = request(self.port, self.source, "/v1/events/route", event,
                             "https://example.invalid")
        self.check("HTTP: first event accepted", accept["status"] == 202, 202, accept)
        valid_duplicate = (duplicate["status"] == 202 and duplicate["body"].get("duplicate") is True
                           and duplicate["body"].get("delivery_id") == accept["body"].get("delivery_id"))
        self.check("HTTP: duplicate is idempotent", valid_duplicate,
                   "202 duplicate with same delivery_id", duplicate)
        self.check("HTTP: conflicting reuse rejected", conflict["status"] == 409, 409, conflict)
        self.check("HTTP: bad credentials rejected", bad_auth["status"] == 401, 401, bad_auth)
        self.check("HTTP: browser origin rejected", bad_origin["status"] == 403, 403, bad_origin)
        self.check("HTTP: one persisted event", len(self.events("route")) == 1, 1,
                   len(self.events("route")))

        paused = self.cli("pause", "route")["json"]
        self.check("HTTP route: pause persisted", paused.get("enabled") is False, False, paused)
        paused_new = request(self.port, self.source, "/v1/events/route", {**event, "id": "paused-new"})
        paused_duplicate = request(self.port, self.source, "/v1/events/route", event)
        self.check("HTTP route: paused route rejects new intake", paused_new["status"] >= 400,
                   "rejection", paused_new)
        self.check("HTTP route: paused idempotent lookup",
                   paused_duplicate["status"] == 202 and paused_duplicate["body"].get("duplicate") is True,
                   "202 duplicate", paused_duplicate)
        unpaused = self.cli("unpause", "route")["json"]
        self.check("HTTP route: resume persisted", unpaused.get("enabled") is True, True, unpaused)
        if self.kind == "rust":
            retired = self.cli("remove", "route")["json"]
            self.check("HTTP route: remove persisted", retired.get("removed") is True, True, retired)
            retirement = "removed"
        else:
            retired = self.cli("disable", "route")["json"]
            self.check("HTTP route: disable persisted", retired.get("enabled") is False, False, retired)
            retirement = "disabled; route removal command unavailable"
        delivery_id = accept["body"]["delivery_id"]
        evidence = self.cli("event", delivery_id)["json"]
        observed_id = evidence.get("delivery_id", evidence.get("id"))
        self.check("HTTP route: removal preserves evidence", observed_id == delivery_id,
                   delivery_id, observed_id)
        self.stop()
        self.start()
        restarted = request(self.port, self.source, "/v1/events/route", event)
        valid_restart = (restarted["status"] == 202 and restarted["body"].get("duplicate") is True
                         and restarted["body"].get("delivery_id") == delivery_id)
        self.check("HTTP route: restart preserves idempotency", valid_restart,
                   "202 duplicate with original delivery_id", restarted)
        return {"statuses": {"accept": accept["status"], "duplicate": duplicate["status"],
                             "conflict": conflict["status"], "bad_auth": bad_auth["status"],
                             "bad_origin": bad_origin["status"], "paused_new": paused_new["status"],
                             "restart_duplicate": restarted["status"]}, "persisted_events": 1,
                "retirement": retirement}

    def run(self):
        result = {"runtime": self.kind, "state": str(self.state), "checks": self.checks,
                  "passed": False}
        try:
            self.initialize()
            result["managed_predicate"] = self.predicate_probe()
            result["path_policy"] = self.path_policy_probe()
            result["conversation_isolation"] = self.isolation_probe()
            result["http"] = self.http_probe()
            result["passed"] = True
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            self.stop()
        return result


def compare(results):
    runtimes = {item["runtime"]: item for item in results}
    python, rust = runtimes.get("python", {}), runtimes.get("rust", {})
    comparisons, differences = [], []
    if not (python.get("passed") and rust.get("passed")):
        return comparisons, differences
    predicate_keys = ("events", "transitions", "envelope_contract")
    left = {key: python["managed_predicate"][key] for key in predicate_keys}
    right = {key: rust["managed_predicate"][key] for key in predicate_keys}
    comparisons.append({"behavior": "managed predicate transitions", "equivalent": left == right,
                        "python": left, "rust": right})
    path_shape = lambda value: {key: {"configuration_accepted": item["configuration_accepted"],
                                      "regular_file_recovery": item["regular_file_recovery"]}
                                for key, item in value.items()}
    left, right = path_shape(python["path_policy"]), path_shape(rust["path_policy"])
    comparisons.append({"behavior": "path input policy", "equivalent": left == right,
                        "python": left, "rust": right})
    for label, key in (("conversation event isolation", "conversation_isolation"),):
        left, right = python[key], rust[key]
        comparisons.append({"behavior": label, "equivalent": left == right,
                            "python": left, "rust": right})
    left, right = python["http"]["statuses"], rust["http"]["statuses"]
    comparisons.append({"behavior": "HTTP status contract", "equivalent": left == right,
                        "python": left, "rust": right})
    if left != right:
        differences.append({"area": "paused route rejection status",
                            "classification": "HTTP contract difference",
                            "python": left["paused_new"], "rust": right["paused_new"],
                            "behavioral_impact": "both reject new intake while paused; status codes differ"})
    py_error = python["path_policy"]["symlink"]["baseline"].get("error")
    rs_error = rust["path_policy"]["symlink"]["baseline"].get("error")
    if py_error != rs_error:
        differences.append({"area": "symlink sample error category",
                            "classification": "diagnostic representation difference",
                            "python": py_error, "rust": rs_error,
                            "behavioral_impact": "none observed; both refused link traversal and recovered"})
    differences.append({"area": "invalid JSON observation surface", "classification": "observability gap",
                        "python": "per-watch last_sample_error",
                        "rust": "process-wide collector sample_errors counter",
                        "behavioral_impact": "both recovered; Rust lacks a per-watch error in watch status"})
    py_keys = python["managed_predicate"]["top_level_keys"]
    rs_keys = rust["managed_predicate"]["top_level_keys"]
    if py_keys != rs_keys:
        differences.append({"area": "managed event top-level envelope keys",
                            "classification": "representation difference",
                            "python": py_keys, "rust": rs_keys,
                            "behavioral_impact": "source, type, and data shape match; optional top-level fields differ"})
    if python["http"]["retirement"] != rust["http"]["retirement"]:
        differences.append({"area": "route removal", "classification": "functionality gap",
                            "python": python["http"]["retirement"],
                            "rust": rust["http"]["retirement"],
                            "behavioral_impact": "both preserve evidence and duplicate identity after restart; only Rust retires the route"})
    return comparisons, differences


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust", required=True)
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--parent", default="/tmp")
    parser.add_argument("--output", default="/tmp/codex-monitor-functional-compare.json")
    arguments = parser.parse_args()
    results = [RuntimeHarness("python", pathlib.Path(arguments.python), arguments.parent).run(),
               RuntimeHarness("rust", pathlib.Path(arguments.rust), arguments.parent).run()]
    comparisons, differences = compare(results)
    passed = all(item.get("passed") for item in results) and all(
        item["equivalent"] for item in comparisons
    )
    report = {
        "generated_at": time.time(), "passed": passed,
        "scope": ["HTTP auth, dedup, conflict, pause/resume, remove evidence, and restart",
                  "managed predicate baseline, debounce, invalid recovery, pause/resume, and restart",
                  "symlink and FIFO policy with regular-file recovery",
                  "cross-conversation managed event isolation"],
        "results": results, "comparisons": comparisons, "differences": differences,
        "limitations": ["No model or operational conversation was invoked.",
                        "The owner endpoint was unavailable; native consumption and model work were not tested.",
                        "Status schemas are compared only where they represent the same contract.",
                        "Event assertions are binding-scoped; global totals are not used as watch evidence."],
    }
    output = pathlib.Path(arguments.output)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(output)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
