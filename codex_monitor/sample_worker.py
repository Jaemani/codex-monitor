"""Owned subprocess transport for one bounded file sample."""

import importlib
import json
import os
import select
import subprocess
import sys
import threading


class SampleProcess:
    """Keep an OS child handle even when its interpreter cannot initialize."""

    def __init__(self, child):
        self.child = child

    @property
    def pid(self):
        return self.child.pid

    def is_alive(self):
        return self.child.poll() is None

    def join(self, timeout=None):
        try:
            self.child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self):
        self.child.terminate()

    def kill(self):
        self.child.kill()

    def close(self):
        for stream in (self.child.stdin, self.child.stdout):
            stream.close()


class SampleResult:
    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit

    def poll(self):
        return bool(select.select([self.stream], [], [], 0)[0])

    def recv(self):
        # Called only after exit and readiness. Never wait for EOF: a stray
        # descendant retaining stdout must not block the supervisor.
        raw = os.read(self.stream.fileno(), self.limit + 1)
        if not raw or len(raw) > self.limit:
            raise ValueError("invalid sample result size")
        return json.loads(raw)

    def close(self):
        self.stream.close()


def launch(path, max_bytes, max_seconds, sampler, condition):
    job = json.dumps({
        "path": str(path), "max_bytes": max_bytes, "max_seconds": max_seconds,
        "sampler_module": sampler.__module__, "sampler_name": sampler.__qualname__,
        "condition": condition,
    }, allow_nan=False)
    # Preserve the caller's import roots for source checkouts and installed
    # runtimes alike. No pickle or fallible parent-to-child bootstrap writes.
    bootstrap = (
        "import sys; sys.path[:0] = " + repr(sys.path) + "; "
        "from codex_monitor.sample_worker import main; main()"
    )
    return subprocess.Popen(
        [sys.executable, "-c", bootstrap, job], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def main():
    from .managed import MAX_SAMPLE_RESULT_BYTES, safe_json_sample

    # stdin is a lifetime lease, not a job stream. Receiver death closes it.
    def guard():
        try:
            os.read(0, 1)
        finally:
            os._exit(0)

    threading.Thread(target=guard, daemon=True).start()
    job = json.loads(sys.argv[1])
    try:
        if job["condition"] is not None:
            result = safe_json_sample(job["path"], job["max_bytes"], job["max_seconds"], job["condition"])
        else:
            sampler = importlib.import_module(job["sampler_module"])
            for name in job["sampler_name"].split("."):
                sampler = getattr(sampler, name)
            result = sampler(job["path"], job["max_bytes"], job["max_seconds"])
    except BaseException as exc:
        result = {"path": job["path"], "state": "unreadable", "error": "worker_exception:" + type(exc).__name__}
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_SAMPLE_RESULT_BYTES:
            raise ValueError("sample too large")
    except (TypeError, ValueError, RecursionError):
        encoded = b'{"state":"unreadable","error":"worker_result"}'
    try:
        while encoded:
            encoded = encoded[os.write(1, encoded):]
    finally:
        os._exit(0)
