"""Supervised, change-only file producers for the explicit monitor registry."""
import hashlib
import os
from pathlib import Path
import stat
import threading
import time

from .monitor import MANAGED_SOURCE
from .watch import ChangeWatcher

MAX_FILE_BYTES = 8 * 1024 * 1024
READ_CHUNK = 1024 * 1024
DEFAULT_POLL_INTERVAL = .25
MAX_READ_SECONDS = .25
TRANSIENT_SAMPLE_ERRORS = {"changed_during_read", "short_read", "read_timeout"}


def safe_file_sample(path, max_bytes=MAX_FILE_BYTES, max_seconds=MAX_READ_SECONDS):
    """Hash one regular file without following symlinks or opening FIFOs."""
    path = Path(path)
    display_path = str(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK prevents a platform-specific special file from blocking before
    # fstat can reject it. Regular-file reads remain finite and bounded below.
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
            # Atomic replacement is deliberate: the next poll opens the new
            # inode. In-place writes during a read are retried as one sample.
            return {"path": display_path, "state": "unreadable", "error": "changed_during_read"}
        return {"path": display_path, "state": "present", "sha256": digest.hexdigest()}
    except OSError as exc:
        return {"path": display_path, "state": "unreadable", "error": type(exc).__name__}
    finally:
        os.close(descriptor)


class ManagedSupervisor:
    """Own all managed sampler work under the lifetime of one ``serve``."""

    def __init__(self, monitor, *, poll_interval=DEFAULT_POLL_INTERVAL, clock=time.monotonic):
        self.monitor = monitor
        self.poll_interval = poll_interval
        self.clock = clock
        self.stop = threading.Event()
        self.thread = None
        self._next = {}
        self._watchers = {}
        self._last_delivery = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, name="codex-monitor-managed", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(1, self.poll_interval * 4))
        for watch_id in list(self._watchers):
            try:
                self.monitor.managed_set_worker(watch_id, "stopped")
            except Exception:
                pass
        self._watchers.clear()
        self._next.clear()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # A registry or filesystem failure must not terminate serve.
                pass
            self.stop.wait(self.poll_interval)
        for watch_id in list(self._watchers):
            try:
                self.monitor.managed_set_worker(watch_id, "stopped")
            except Exception:
                pass

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

    def poll_once(self):
        rows = self.monitor.managed_runtime_rows()
        active_ids = {row["id"] for row in rows}
        for watch_id in list(self._watchers):
            if watch_id not in active_ids:
                self._watchers.pop(watch_id, None)
                self._next.pop(watch_id, None)
        now = self.clock()
        for row in rows:
            if self.stop.is_set():
                break
            watch_id = row["id"]
            if not row["enabled"] or row["removed"]:
                self._watchers.pop(watch_id, None)
                self._next.pop(watch_id, None)
                try:
                    self.monitor.managed_set_worker(watch_id, "stopped")
                except Exception:
                    pass
                continue
            self.monitor.managed_heartbeat(watch_id)
            if now < self._next.get(watch_id, 0):
                continue
            # Re-read after the bounded registry scan. A concurrent pause or
            # removal is therefore observed before sampling or ingesting.
            current = self.monitor.managed_runtime_row(watch_id)
            if current is None or not current["enabled"] or current["removed"]:
                self._watchers.pop(watch_id, None)
                self._next.pop(watch_id, None)
                continue
            self.monitor.managed_set_worker(watch_id, "running")
            try:
                sample = safe_file_sample(current["path"])
                sample_error = None
                if sample.get("state") == "unreadable":
                    sample_error = "file sample: " + str(sample.get("error", "unreadable"))
                    if sample.get("error") in TRANSIENT_SAMPLE_ERRORS:
                        self.monitor.managed_set_worker(watch_id, "running", sample_error, sample_error=sample_error)
                        self._next[watch_id] = now + current["interval"]
                        continue
                self._watcher(current).check(sample)
                self.monitor.managed_set_worker(
                    watch_id, "running", sample_error, self._last_delivery.pop(watch_id, None),
                    sample_error=sample_error,
                )
            except Exception as exc:
                self.monitor.managed_set_worker(watch_id, "running", str(exc)[:500])
            self._next[watch_id] = now + current["interval"]
