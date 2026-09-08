#!/usr/bin/env python3
"""One HTTP check for an OS timer; emit one event per confirmed outage.

Requires the installed codex-monitor executable and a configured external
source/binding. A private SQLite checkpoint preserves failure counts and event
identity across invocations. This example checks HTTP status, not application
correctness, and never performs repairs itself.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check(url, timeout):
    """A 200 response is healthy; redirects and other statuses are failures."""
    try:
        with build_opener(NoRedirect, ProxyHandler({})).open(Request(url), timeout=timeout) as response:
            return response.status == 200
    except HTTPError as exc:
        exc.close()
        return False
    except (URLError, OSError, ValueError):
        return False


def run(args):
    parsed = urlsplit(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("use an HTTP(S) URL without embedded credentials")
    parsed.port  # Reject malformed or out-of-range ports as configuration errors.
    if any(ord(char) <= 32 or ord(char) == 127 for char in args.url):
        raise ValueError("URL must not contain whitespace or control characters")
    if not Path(args.monitor_bin).is_absolute():
        raise ValueError("monitor launcher must be an absolute path")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30 or args.failures < 1:
        raise ValueError("timeout must be 0 < seconds <= 30; failures must be positive")
    checkpoint = Path(args.checkpoint).expanduser().resolve()
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A non-overlapping OS job is required. A concurrent invocation fails on
    # the short lock deadline rather than accumulating queued health checks.
    monitor_state = str(Path(args.monitor_state).expanduser().resolve())
    identity = json.dumps([args.url, args.binding, args.source, monitor_state, args.failures])
    db = sqlite3.connect(checkpoint, timeout=1)
    try:
        os.chmod(checkpoint, 0o600)
        db.execute("CREATE TABLE IF NOT EXISTS probe (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL, failures INTEGER NOT NULL, event_id TEXT, observed REAL, delivered INTEGER NOT NULL)")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT identity,failures,event_id,observed,delivered FROM probe WHERE singleton=1").fetchone()
        if row and row[0] != identity:
            raise ValueError("checkpoint belongs to a different check; use a separate checkpoint")
        healthy = check(args.url, args.timeout)
        failures = 0 if healthy else (row[1] if row else 0) + 1
        event_id = row[2] if row and not healthy else None
        observed = row[3] if row and not healthy else None
        delivered = row[4] if row and not healthy else 0
        if failures >= args.failures and event_id is None:
            event_id, observed = str(uuid.uuid4()), time.time()
        db.execute("INSERT OR REPLACE INTO probe VALUES(1,?,?,?,?,?)", (identity, failures, event_id, observed, delivered))
        db.commit()
        if healthy or event_id is None or delivered:
            return 0
        # Commit the ID before submission. If the process dies after acceptance,
        # the next failing check retries exactly the same envelope and ID.
        # After a confirmed send, later failed checks stay quiet until recovery.
        data = json.dumps({"message": "HTTP health check failed; inspect the service and apply only the authorized recovery procedure.", "observed_at": observed, "check": parsed.hostname})
        result = subprocess.run(
            [args.monitor_bin, "--state", monitor_state, "send", "--to", args.binding,
             "--source", args.source, "--type", "service.unhealthy", "--id", event_id, "--data", data],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
        if result.returncode == 0:
            db.execute("UPDATE probe SET delivered=1 WHERE singleton=1 AND event_id=?", (event_id,))
            db.commit()
            return 0
        return 2
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--checkpoint", required=True, help="private producer SQLite checkpoint, one per check")
    p.add_argument("--monitor-bin", required=True, help="absolute installed codex-monitor launcher")
    p.add_argument("--monitor-state", required=True)
    p.add_argument("--binding", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--failures", type=int, default=3, help="consecutive failed invocations before notification")
    p.add_argument("--timeout", type=float, default=3)
    args = p.parse_args()
    try:
        return run(args)
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Do not echo endpoints, credentials or receiver output into timer logs.
        print("health-hook: check/delivery failed (" + type(exc).__name__ + ")", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
