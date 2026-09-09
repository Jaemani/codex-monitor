#!/usr/bin/env python3
"""Matched collector resource observations; use disposable state and local reports."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def tree_stats(root_pid, seen):
    rows = []
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
    for line in output.splitlines():
        try:
            pid, parent, rss = map(int, line.split())
            rows.append((pid, parent, rss))
        except ValueError:
            continue
    pids = {root_pid}
    while True:
        expanded = pids | {pid for pid, parent, _ in rows if parent in pids}
        if expanded == pids:
            break
        pids = expanded
    actual = {pid for pid, _, _ in rows if pid in pids}
    seen.update(actual)
    return sum(rss for pid, _, rss in rows if pid in actual)


def db_counts(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    wal = Path(str(path) + "-wal")
    return {"events": events, "db_bytes": path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0}


def baseline_count(state, runtime, digest):
    if runtime == "python":
        samples = []
        for path in (state / "managed").glob("*.json"):
            try:
                samples.append(json.loads(path.read_text()).get("last"))
            except (OSError, ValueError):
                pass
    else:
        with sqlite3.connect(f"file:{state / 'rust.sqlite3'}?mode=ro", uri=True) as conn:
            samples = [json.loads(row[0]) for row in conn.execute("SELECT checkpoint FROM managed_watches WHERE checkpoint IS NOT NULL")]
    return sum(isinstance(s, dict) and s.get("state") == "present" and s.get("sha256") == digest for s in samples)


def stop_with_usage(proc):
    if proc.returncode is not None:
        return {"clean_exit": False, "whole_run_waited_cpu_seconds": None}
    os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
            return {"clean_exit": proc.returncode == 0,
                    "whole_run_waited_cpu_seconds": usage.ru_utime + usage.ru_stime,
                    "receiver_exit_code": proc.returncode}
        if time.monotonic() > deadline:
            os.killpg(proc.pid, signal.SIGKILL)
        time.sleep(.05)


def one(runtime, executable, watches, duration, interval, settle_seconds):
    result = {"runtime": runtime, "watches": watches, "interval_seconds": interval,
              "phase_seconds": duration, "status": "RUNNING"}
    with tempfile.TemporaryDirectory(prefix=f"cm-resource-{runtime}-{watches}-") as directory:
        state = Path(directory)
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env["PYTHONPATH"] = str(ROOT)
        env.pop("CODEX_MONITOR_SAMPLE_WORKER", None)
        command = [str(executable)] if runtime == "rust" else [str(executable), "-m", "codex_monitor"]
        def cli(*args):
            call = subprocess.run(command + ["--state", str(state), *args], env=env,
                                  capture_output=True, text=True, timeout=30)
            if call.returncode:
                raise RuntimeError(call.stderr[-1500:])
            return json.loads(call.stdout)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cli("init", "--port", str(port))
        original = json.dumps({"value": 0, "padding": "x" * 4050}).encode()
        changed = json.dumps({"value": 1, "padding": "x" * 4050}).encode()
        result["file_bytes"] = len(original)
        paths = []
        for i in range(watches):
            path = state / f"watch-{i}.json"
            path.write_bytes(original)
            paths.append(path)
            cli("monitor", "create", f"w{i}", "--thread", f"bench-{i // 32}",
                "--file", str(path), "--interval", str(interval), "--endpoint", "ws://127.0.0.1:1")
        db = state / ("monitor.sqlite3" if runtime == "python" else "rust.sqlite3")
        seen = set()
        with (state / "stderr.log").open("w+") as log:
            proc = subprocess.Popen(command + ["--state", str(state), "serve"], env=env,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            def alive():
                if proc.poll() is not None:
                    log.flush()
                    raise RuntimeError(f"receiver exited {proc.returncode}: {(state / 'stderr.log').read_text()[-1500:]}")
            def phase(name):
                samples = []
                before = db_counts(db)
                born_before = len(seen)
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    alive()
                    samples.append(tree_stats(proc.pid, seen))
                    time.sleep(.2)
                ordered = sorted(samples)
                return {"samples": len(samples), "rss_kib_median": statistics.median(samples),
                        "rss_kib_p95": ordered[min(len(ordered)-1, int(.95*len(ordered)))],
                        "rss_kib_peak": max(samples), "sampled_process_births": len(seen)-born_before,
                        "before": before, "after": db_counts(db)}
            try:
                digest = hashlib.sha256(original).hexdigest()
                deadline = time.monotonic() + settle_seconds
                while True:
                    alive()
                    count = baseline_count(state, runtime, digest)
                    if count == watches and cli("status").get("receiver", {}).get("ready", runtime == "python"):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"baseline incomplete: {count}/{watches} within {settle_seconds}s")
                    time.sleep(.2)
                result["verified_baselines"] = count
                result["idle"] = phase("idle")
                if result["idle"]["after"]["events"] != 0:
                    raise RuntimeError("unchanged baseline generated events")
                for path in paths:
                    path.write_bytes(changed)
                result["burst"] = phase("burst")
                result["expected_events"] = watches
                result["actual_events"] = result["burst"]["after"]["events"]
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                    per_binding = conn.execute("SELECT binding,count(*) FROM events GROUP BY binding").fetchall()
                result["bindings_with_one_event"] = sum(count == 1 for _, count in per_binding)
                result["changed_checkpoints"] = baseline_count(state, runtime, hashlib.sha256(changed).hexdigest())
                result["no_loss"] = (result["actual_events"] == watches and result["bindings_with_one_event"] == watches and result["changed_checkpoints"] == watches)
                result["status"] = "PASS" if result["no_loss"] else "FAIL"
            except Exception as error:
                result["status"] = "ERROR"
                result["error"] = str(error)
            finally:
                result.update(stop_with_usage(proc))
                if not result["clean_exit"] and result["status"] == "PASS":
                    result["status"] = "FAIL"
        result["temporary_state_removed"] = True
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rust", type=Path, required=True)
    p.add_argument("--python", type=Path, default=Path(sys.executable))
    p.add_argument("--counts", default="0,10,50,128")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--settle-seconds", type=float, default=90)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.resolve().is_relative_to(ROOT):
        p.error("raw reports must be outside Git")
    report = {"results": [], "scope": "matched local collector process trees; unavailable owner; no model",
              "rust_sha256": hashlib.sha256(args.rust.read_bytes()).hexdigest(),
              "python_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "caveats": ["RSS includes all sampled descendants and double-counts shared pages.",
                          "200ms process sampling can miss short-lived workers and underestimate peaks/births.",
                          "wait4 CPU includes the receiver and descendants it reaped, across startup, both phases and shutdown; not per-phase CPU.",
                          "DB/WAL sizes are allocated bytes, not total disk writes; no write-amplification claim.",
                          "Native delivery latency and App Server memory are excluded: the endpoint is intentionally unavailable."]}
    for count in map(int, args.counts.split(",")):
        for runtime, executable in (("python", args.python.resolve()), ("rust", args.rust.resolve())):
            try:
                if hashlib.sha256(args.rust.read_bytes()).hexdigest() != report["rust_sha256"]:
                    raise RuntimeError("Rust executable changed during the matrix; rerun with a frozen binary")
                result = one(runtime, executable, count, args.duration, args.interval, args.settle_seconds)
                if hashlib.sha256(args.rust.read_bytes()).hexdigest() != report["rust_sha256"]:
                    result["status"]="ERROR"
                    result["error"]="Rust executable changed during the case; discard this observation"
            except Exception as error:
                result = {"runtime": runtime, "watches": count, "status": "ERROR", "error": str(error)}
            report["results"].append(result)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(result), flush=True)
    return 0 if all(r["status"] == "PASS" for r in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
