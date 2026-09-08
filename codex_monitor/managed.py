"""Supervised, change-only file producers for the explicit monitor registry.

File reads run in short-lived child processes. The supervisor owns every
registry, checkpoint, and delivery mutation; a sampler can return only a
plain sample value. A child also holds a lifetime pipe whose other end is
owned by the supervisor, so an abrupt receiver exit closes the pipe and the
child exits without relying on ``Process.daemon``. An OS read stuck in an
unkillable kernel state remains outside Python's guarantees; bounded worker
accounting prevents that case from creating an unbounded process leak.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any, Callable

from .conditions import ConditionDebouncer
from .monitor import MANAGED_SOURCE
from .watch import ChangeWatcher

MAX_FILE_BYTES = 8 * 1024 * 1024
READ_CHUNK = 1024 * 1024
DEFAULT_POLL_INTERVAL = .25
MAX_READ_SECONDS = .25
MAX_SAMPLE_WORKERS = 8
MAX_SAMPLE_WORKERS_PER_THREAD = 2
PARENT_SAMPLE_DEADLINE_SECONDS = 2.0
MAX_SAMPLE_RESULT_BYTES = 4096
TRANSIENT_SAMPLE_ERRORS = {
    "changed_during_read", "short_read", "read_timeout", "worker_timeout",
    "worker_exited", "worker_exception", "worker_result",
}


def safe_file_sample(path, max_bytes=MAX_FILE_BYTES, max_seconds=MAX_READ_SECONDS):
    """Hash one regular file without following symlinks or opening FIFOs."""
    path = Path(path)
    display_path = str(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {"path": display_path, "state": "missing"}
    except OSError as exc:
        return {"path": display_path, "state": "unreadable", "error": type(exc).__name__}
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return {"path": display_path, "state": "unreadable", "error": "not_regular_file"}
        if before.st_size > max_bytes:
            return {"path": display_path, "state": "unreadable", "error": "file_too_large"}
        digest = hashlib.sha256()
        remaining = before.st_size
        deadline = time.monotonic() + max_seconds
        while remaining:
            if time.monotonic() >= deadline:
                return {"path": display_path, "state": "unreadable", "error": "read_timeout"}
            block = os.read(descriptor, min(READ_CHUNK, remaining))
            if not block:
                return {"path": display_path, "state": "unreadable", "error": "short_read"}
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(descriptor)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after):
            return {"path": display_path, "state": "unreadable", "error": "changed_during_read"}
        return {"path": display_path, "state": "present", "sha256": digest.hexdigest()}
    except OSError as exc:
        return {"path": display_path, "state": "unreadable", "error": type(exc).__name__}
    finally:
        os.close(descriptor)


def _parent_lifetime_guard(connection, finished):
    """Exit the child if the supervisor closes its lifetime pipe."""
    try:
        connection.recv_bytes()
    except (EOFError, OSError):
        if not finished.is_set():
            os._exit(0)


def _sample_worker(connection, lifetime, path, max_bytes, max_seconds, sampler):
    """Run a sampler without any monitor or checkpoint access."""
    finished = threading.Event()
    guard = threading.Thread(
        target=_parent_lifetime_guard, args=(lifetime, finished),
        name="codex-monitor-sample-parent-guard", daemon=True,
    )
    guard.start()
    try:
        try:
            result = sampler(path, max_bytes, max_seconds)
        except BaseException as exc:
            result = {
                "path": str(path), "state": "unreadable",
                "error": "worker_exception:" + type(exc).__name__,
            }
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode()
        except (TypeError, ValueError, RecursionError):
            encoded = b""
        if not encoded or len(encoded) > MAX_SAMPLE_RESULT_BYTES:
            # Do not echo an unbounded path in the capped fallback. The
            # supervisor already knows the expected path and treats this
            # missing-path result as ``worker_result``.
            result = {"state": "unreadable", "error": "worker_result"}
        try:
            connection.send(result)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        finished.set()
        try:
            connection.close()
        except OSError:
            pass
        try:
            lifetime.close()
        except OSError:
            pass
        # The lifetime guard is deliberately blocked while sampling. Closing
        # the child end from another thread is not guaranteed to wake that
        # reader on every Python/POSIX combination. The result write above is
        # synchronous, so exit the worker explicitly after closing both ends.
        os._exit(0)


@dataclass
class _SampleWorker:
    watch_id: str
    thread: str
    lifecycle_epoch: int
    process: Any
    result: Any
    lifetime: Any
    started: float
    deadline: float
    cancelled: bool = False
    reported: bool = False


class ManagedSupervisor:
    """Own all managed sampler work under the lifetime of one ``serve``."""

    def __init__(
        self, monitor, *, poll_interval=DEFAULT_POLL_INTERVAL, clock=time.monotonic,
        max_workers=MAX_SAMPLE_WORKERS,
        max_workers_per_thread=MAX_SAMPLE_WORKERS_PER_THREAD,
        sample_timeout=PARENT_SAMPLE_DEADLINE_SECONDS,
        sampler: Callable = safe_file_sample,
        context=None,
        monotonic=time.monotonic,
    ):
        if (isinstance(poll_interval, bool) or
                not isinstance(poll_interval, (int, float)) or
                not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError("poll interval must be positive")
        if type(max_workers) is not int or max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        if type(max_workers_per_thread) is not int or max_workers_per_thread <= 0:
            raise ValueError("max_workers_per_thread must be a positive integer")
        if (isinstance(sample_timeout, bool) or
                not isinstance(sample_timeout, (int, float)) or
                not math.isfinite(sample_timeout) or sample_timeout <= 0):
            raise ValueError("sample_timeout must be positive")
        if not callable(sampler):
            raise ValueError("sampler must be callable")
        self.monitor = monitor
        self.poll_interval = float(poll_interval)
        self.clock = clock
        self.monotonic = monotonic
        self.max_workers = max_workers
        self.max_workers_per_thread = max_workers_per_thread
        self.sample_timeout = float(sample_timeout)
        self.sampler = sampler
        self.context = context or multiprocessing.get_context("spawn")
        self.stop = threading.Event()
        self.thread = None
        self._next = {}
        self._watchers = {}
        self._policies = {}
        self._last_delivery = {}
        self._workers: dict[str, _SampleWorker] = {}
        self._fair_cursor = 0
        self._state_lock = threading.RLock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, name="codex-monitor-managed", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=max(1, self.sample_timeout + self.poll_interval * 2))
        with self._state_lock:
            for worker in list(self._workers.values()):
                self._cancel_worker(worker)
            self._reap_workers(self.clock())
            for watch_id in list(self._watchers):
                try:
                    self.monitor.managed_set_worker(watch_id, "stopped")
                except Exception:
                    pass
            self._watchers.clear()
            self._policies.clear()
            self._next.clear()
            self._last_delivery.clear()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self.stop.wait(self.poll_interval)
        with self._state_lock:
            for worker in list(self._workers.values()):
                self._cancel_worker(worker)
            self._reap_workers(self.clock())

    @staticmethod
    def _epoch(row):
        value = row.get("lifecycle_epoch", 0)
        return value if type(value) is int else 0

    def _watcher(self, row):
        watch_id = row["id"]
        watcher = self._watchers.get(watch_id)
        if watcher is not None:
            return watcher
        checkpoint = self.monitor._managed_checkpoint(self.monitor.root, watch_id)

        def emit(event):
            receipt = self.monitor.ingest(row["binding"], event)
            self._last_delivery[watch_id] = receipt["delivery_id"]
            return receipt

        watcher = ChangeWatcher(
            checkpoint,
            MANAGED_SOURCE,
            "monitor.file.changed",
            emit,
            event_builder=lambda event: {
                **event,
                "data": {
                    "watch": row["name"], "path": row["path"],
                    "previous": event["data"]["previous"], "current": event["data"]["current"],
                },
            },
        )
        self._watchers[watch_id] = watcher
        return watcher

    def _observe_sample(self, row, sample):
        watcher = self._watcher(row)
        if not row.get("debounce_seconds", 0):
            return watcher.check(sample)
        checkpoint, error = self.monitor._managed_checkpoint_state(row["id"])
        if error:
            raise ValueError(error)
        # An already durable event must be replayed before a newer sample can
        # cancel or replace a condition candidate.
        if checkpoint and checkpoint.get("pending"):
            watcher.check(checkpoint["pending"]["data"]["current"])
            checkpoint, error = self.monitor._managed_checkpoint_state(row["id"])
            if error:
                raise ValueError(error)
        policy = self._policies.get(row["id"])
        if policy is None:
            policy = ConditionDebouncer(
                self.monitor._managed_condition_checkpoint(self.monitor.root, row["id"]),
                row["debounce_seconds"], clock=self.monotonic,
            )
            self._policies[row["id"]] = policy
        selected = policy.select(
            sample, checkpoint["last"] if checkpoint else None,
            pause_epoch=self._epoch(row),
        )
        if selected is None:
            return False
        changed = watcher.check(selected)
        policy.commit(selected)
        return changed

    def _start_worker(self, row):
        result_parent, result_child = self.context.Pipe(False)
        lifetime_parent, lifetime_child = self.context.Pipe(True)
        process = self.context.Process(
            target=_sample_worker,
            args=(result_child, lifetime_child, row["path"], MAX_FILE_BYTES, MAX_READ_SECONDS, self.sampler),
            name="codex-monitor-file-sample",
        )
        process.daemon = True
        try:
            process.start()
        except Exception:
            for connection in (result_parent, result_child, lifetime_parent, lifetime_child):
                try:
                    connection.close()
                except OSError:
                    pass
            raise
        result_child.close()
        lifetime_child.close()
        try:
            # A crashed child can leave a partial frame in the result pipe.
            # Nonblocking reads let the parent classify that as an exited
            # worker instead of hanging after the hard deadline.
            os.set_blocking(result_parent.fileno(), False)
        except (AttributeError, OSError):
            pass
        started = self.monotonic()
        self._workers[row["id"]] = _SampleWorker(
            watch_id=row["id"], thread=row["thread"], lifecycle_epoch=self._epoch(row), process=process,
            result=result_parent, lifetime=lifetime_parent, started=started,
            deadline=started + self.sample_timeout,
        )
        return self._workers[row["id"]]

    @staticmethod
    def _close_connection(connection):
        try:
            connection.close()
        except (OSError, ValueError):
            pass

    def _terminate_process(self, worker):
        self._close_connection(worker.lifetime)
        process = worker.process
        try:
            alive = process.is_alive()
        except (OSError, ValueError):
            alive = False
        if alive:
            process.terminate()
            process.join(timeout=min(.1, self.sample_timeout))
        try:
            alive = process.is_alive()
        except (OSError, ValueError):
            alive = False
        if alive and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=min(.1, self.sample_timeout))

    def _retire_if_dead(self, worker):
        try:
            alive = worker.process.is_alive()
        except (OSError, ValueError):
            alive = False
        if alive:
            return False
        try:
            worker.process.join(timeout=0)
        except (OSError, ValueError):
            pass
        self._close_connection(worker.result)
        self._close_connection(worker.lifetime)
        try:
            worker.process.close()
        except (OSError, ValueError):
            pass
        self._workers.pop(worker.watch_id, None)
        return True

    def _cancel_worker(self, worker):
        if worker.cancelled:
            self._retire_if_dead(worker)
            return
        worker.cancelled = True
        self._terminate_process(worker)
        self._retire_if_dead(worker)

    def _row_is_current(self, worker, row):
        return bool(
            row and row.get("id") == worker.watch_id and row.get("enabled") and
            not row.get("removed") and self._epoch(row) == worker.lifecycle_epoch
        )

    def _sample_error(self, row, message):
        self._policies.pop(row["id"], None)
        try:
            self.monitor.managed_set_worker(row["id"], "running", message, sample_error=message)
        except Exception:
            pass

    def _apply_result(self, worker, row, sample, now):
        if not self._row_is_current(worker, row):
            return
        if not isinstance(sample, dict) or sample.get("path") != row["path"]:
            self._sample_error(row, "file sample: worker_result")
            self._next[row["id"]] = now + row["interval"]
            return
        sample_error = None
        if sample.get("state") == "unreadable":
            sample_error = "file sample: " + str(sample.get("error", "unreadable"))
            if sample.get("error") in TRANSIENT_SAMPLE_ERRORS or str(sample.get("error", "")).startswith("worker_"):
                self._sample_error(row, sample_error)
                self._next[row["id"]] = now + row["interval"]
                return
        try:
            current = self.monitor.managed_runtime_row(worker.watch_id)
            if not self._row_is_current(worker, current):
                return
            self._observe_sample(current, sample)
            self.monitor.managed_set_worker(
                worker.watch_id, "running", sample_error,
                self._last_delivery.pop(worker.watch_id, None), sample_error=sample_error,
            )
        except Exception as exc:
            self.monitor.managed_set_worker(
                worker.watch_id, "running", str(exc)[:500], sample_error=sample_error,
            )
        self._next[row["id"]] = now + row["interval"]

    def _reap_workers(self, now, real_now=None):
        real_now = self.monotonic() if real_now is None else real_now
        for worker in list(self._workers.values()):
            if worker.cancelled or worker.reported:
                self._cancel_worker(worker)
                continue
            row = self.monitor.managed_runtime_row(worker.watch_id)
            if not self._row_is_current(worker, row):
                self._cancel_worker(worker)
                continue
            try:
                alive = worker.process.is_alive()
            except (OSError, ValueError):
                alive = False
            if not alive:
                if worker.result.poll():
                    try:
                        sample = worker.result.recv()
                    except (BlockingIOError, EOFError, OSError, ValueError):
                        sample = {"path": row["path"], "state": "unreadable", "error": "worker_exited"}
                    worker.reported = True
                    self._apply_result(worker, row, sample, now)
                else:
                    worker.reported = True
                    self._sample_error(row, "file sample: worker_exited")
                    self._next[worker.watch_id] = now + row["interval"]
                self._retire_if_dead(worker)
                continue
            if real_now >= worker.deadline:
                worker.reported = True
                self._sample_error(row, "file sample: worker_timeout")
                self._next[worker.watch_id] = now + row["interval"]
                self._terminate_process(worker)
                self._retire_if_dead(worker)

    def _schedule(self, rows, now):
        if not rows:
            return []
        rows = list(rows)
        start = self._fair_cursor % len(rows)
        ordered = rows[start:] + rows[:start]
        self._fair_cursor = (start + 1) % len(rows)
        scheduled = []
        for row in ordered:
            watch_id = row["id"]
            if not row["enabled"] or row["removed"]:
                self._next.pop(watch_id, None)
                self._watchers.pop(watch_id, None)
                self._policies.pop(watch_id, None)
                self._last_delivery.pop(watch_id, None)
                try:
                    self.monitor.managed_set_worker(watch_id, "stopped")
                except Exception:
                    pass
                continue
            if now < self._next.get(watch_id, 0) or watch_id in self._workers:
                continue
            thread_workers = sum(
                worker.thread == row["thread"] for worker in self._workers.values()
            )
            if thread_workers >= self.max_workers_per_thread:
                self._sample_error(
                    row,
                    f"file sample capacity exhausted (thread limit {self.max_workers_per_thread})",
                )
                self._next[watch_id] = now + max(row["interval"], self.poll_interval)
                continue
            if len(self._workers) >= self.max_workers:
                self._sample_error(row, f"file sample capacity exhausted (global limit {self.max_workers})")
                self._next[watch_id] = now + max(row["interval"], self.poll_interval)
                continue
            try:
                self._start_worker(row)
                scheduled.append(watch_id)
            except Exception as exc:
                self._sample_error(row, "file sample worker_start: " + type(exc).__name__)
                self._next[watch_id] = now + max(row["interval"], self.poll_interval)
        return scheduled

    def poll_once(self):
        with self._state_lock:
            rows = self.monitor.managed_runtime_rows()
            active_ids = {row["id"] for row in rows}
            for watch_id in list(self._watchers):
                if watch_id not in active_ids:
                    self._watchers.pop(watch_id, None)
                    self._policies.pop(watch_id, None)
                    self._next.pop(watch_id, None)
                    self._last_delivery.pop(watch_id, None)
            now = self.clock()
            self._reap_workers(now)
            for row in rows:
                if self.stop.is_set():
                    break
                if row["enabled"] and not row["removed"]:
                    self.monitor.managed_heartbeat(row["id"])
            self._schedule(rows, now)
