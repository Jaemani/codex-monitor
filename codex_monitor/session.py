"""Supported App Server transports; no UI typing, hidden IPC or exec fallback."""
import json
import os
import queue
import subprocess
import threading
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlparse

from .errors import Retryable, Uncertain, Permanent


def _validate_server_token(value, source):
    token = value.strip()
    if not token or len(token) > 65536 or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise Permanent(f"{source} must contain one nonempty printable ASCII bearer token")
    return token


def server_token(environ=None):
    """Return a bearer token without putting credential contents in errors."""
    environ = os.environ if environ is None else environ
    value = environ.get("CODEX_MONITOR_SERVER_TOKEN")
    if value is not None:
        return _validate_server_token(value, "CODEX_MONITOR_SERVER_TOKEN")
    raw_path = environ.get("CODEX_MONITOR_SERVER_TOKEN_FILE")
    if raw_path is None:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE must be an absolute path")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Opening a FIFO read-only can otherwise block before fstat can reject it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Permanent("cannot open CODEX_MONITOR_SERVER_TOKEN_FILE") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE must be a regular file")
        if os.name == "posix":
            if metadata.st_uid != os.getuid():
                raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE must be owned by the current user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE permissions must deny group and other access")
        chunks = []
        remaining = 65537
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > 65536:
            raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE exceeds 64 KiB")
        try:
            value = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise Permanent("CODEX_MONITOR_SERVER_TOKEN_FILE must contain ASCII") from exc
    except OSError as exc:
        raise Permanent("cannot read CODEX_MONITOR_SERVER_TOKEN_FILE") from exc
    finally:
        os.close(descriptor)
    return _validate_server_token(value, "CODEX_MONITOR_SERVER_TOKEN_FILE")


class RpcError(Exception):
    def __init__(self, value):
        self.code = value.get("code")
        self.data = value.get("data")
        super().__init__(value.get("message", "RPC error"))


def _operation_error(operation, exc):
    # Parse/shape/method errors are stable until the caller or server changes.
    # Older thread stores surface a missing rollout as an internal error, so
    # preserve the server's stable target diagnosis across those versions.
    message = str(exc).lower()
    permanent_target_errors = (
        "thread not found:",
        "no rollout found for thread id",
        " is archived",
        "does not support queued submissions",
        "direct app-server input is not allowed",
        "user message queue is unavailable",
    )
    if exc.code in (-32700, -32600, -32601, -32602) or any(
        detail in message for detail in permanent_target_errors
    ):
        return Permanent(f"{operation} rejected: {exc}")
    return Retryable(f"{operation} temporarily failed: {exc}")


def _submission_error(exc):
    classified = _operation_error("queue submission", exc)
    if isinstance(classified, Permanent):
        return classified
    if exc.code == -32001:
        return Retryable(f"queue submission temporarily failed: {exc}")
    # Queue persistence happens before the response is serialized. An internal
    # error can therefore arrive after acceptance; reconcile the stable client
    # id rather than replaying the request.
    return Uncertain(f"queue submission failed after handoff; acceptance unknown: {exc}")


class Rpc:
    def __init__(self, endpoint="local", *, command=None, timeout=10, token=None):
        self.timeout = timeout
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        self.proc = None
        self.ws = None
        self.reader = None
        self.writer = None
        self.outbound = queue.Queue()
        self._close_lock = threading.Lock()
        self._close_started = False
        self._close_done = threading.Event()
        try:
            if command is None and endpoint.startswith("unix:///"):
                from websockets.sync.client import unix_connect
                # The native Unix control-socket upgrader intentionally doesn't
                # negotiate WebSocket extensions. websockets enables deflate by
                # default, which makes that upgrader reject the handshake.
                self.ws = unix_connect(endpoint.removeprefix("unix://"), open_timeout=timeout,
                                       close_timeout=min(timeout, 2), compression=None,
                                       max_size=16*1024*1024)
            elif command is None and endpoint.startswith(("ws://", "wss://")):
                from websockets.sync.client import connect
                parsed = urlparse(endpoint)
                host = parsed.hostname
                if parsed.username or parsed.password or parsed.fragment:
                    raise Permanent("App Server credentials belong in the token environment variable, not the endpoint URL")
                if endpoint.startswith("ws://") and host not in ("127.0.0.1", "localhost", "::1"):
                    raise Permanent("non-loopback App Server connections require wss://")
                headers = {"Authorization": "Bearer " + token} if token else None
                self.ws = connect(endpoint, additional_headers=headers, open_timeout=timeout,
                                  close_timeout=min(timeout, 2), max_size=16*1024*1024)
            else:
                if command is None:
                    command = ["codex", "app-server", "proxy"]
                    if endpoint == "shared-local":
                        # Official queue API, backed by Codex's shared local store.
                        # This process never loads/resumes a thread. The owning UI's
                        # server detects external queue changes and runs its thread.
                        command = ["codex", "app-server", "--listen", "stdio://"]
                    elif endpoint.startswith("ssh://"):
                        alias = endpoint.removeprefix("ssh://")
                        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", alias):
                            raise Permanent("ssh endpoint must contain only a configured SSH host alias")
                        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                                   alias, "codex", "app-server", "proxy"]
                    elif endpoint not in ("local", "unix://"):
                        if not endpoint.startswith("unix:///"):
                            raise Permanent("endpoint must be shared-local, local, ssh://ALIAS, unix:///absolute/path, ws://loopback, or wss://")
                        command += ["--sock", endpoint.removeprefix("unix://")]
                self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self.writer = threading.Thread(target=self._write_loop, name="codex-monitor-rpc-writer", daemon=True)
            self.writer.start()
            self.reader = threading.Thread(target=self._read, name="codex-monitor-rpc", daemon=True)
            self.reader.start()
            self.info = self.call("initialize", {
                "clientInfo": {"name": "codex_monitor", "title": "Codex Monitor", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self._send({"method": "initialized", "params": {}}, time.monotonic() + self.timeout)
        except Permanent:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise Retryable("App Server is unreachable or initialization failed; verify the conversation's endpoint") from exc

    def _write(self, message):
        data = json.dumps(message, ensure_ascii=False)
        if self.ws:
            self.ws.send(data)
        else:
            self.proc.stdin.write(data + "\n")
            self.proc.stdin.flush()

    def _write_loop(self):
        try:
            while True:
                item = self.outbound.get()
                if item is None:
                    return
                message, status = item
                if self.closed:
                    status.put(("not_sent", None))
                    continue
                try:
                    self._write(message)
                except Exception as exc:
                    status.put(("uncertain", exc))
                    return
                status.put(("sent", None))
        finally:
            while True:
                try:
                    item = self.outbound.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[1].put(("not_sent", None))
            self.closed = True
            self.close()

    def _send(self, message, deadline):
        status = queue.Queue(maxsize=1)
        with self.lock:
            if self.closed:
                raise Retryable("App Server connection closed before submission")
            self.outbound.put((message, status))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise Uncertain("App Server write timed out; acceptance unknown")
        try:
            outcome, error = status.get(timeout=remaining)
        except queue.Empty as exc:
            self.close()
            raise Uncertain("App Server write timed out; acceptance unknown") from exc
        if outcome == "not_sent":
            raise Retryable("App Server connection closed before submission")
        if outcome == "uncertain":
            self.close()
            raise Uncertain("App Server write failed; acceptance unknown") from error

    def _read(self):
        try:
            while not self.closed:
                raw = self.ws.recv() if self.ws else self.proc.stdout.readline()
                if not raw:
                    break
                message = json.loads(raw)
                # Approval/elicitation requests belong to the interactive client.
                # This observer never grants permissions or answers user questions.
                if "id" in message and "method" not in message:
                    with self.lock:
                        waiter = self.pending.get(message["id"])
                    if waiter:
                        waiter.put(message)
        except Exception:
            pass
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put(None)
            self.closed = True
            # EOF means the stdio writer is dead (or closed stdout while still
            # alive). Reap or terminate it now instead of leaving it behind
            # until the pool happens to be used again.
            self.close()

    def call(self, method, params, *, timeout=None):
        call_timeout = self.timeout if timeout is None else min(self.timeout, timeout)
        if call_timeout <= 0:
            raise Retryable("App Server call timeout must be positive")
        deadline = time.monotonic() + call_timeout
        with self.lock:
            if self.closed:
                raise Retryable("App Server connection closed before submission")
            self.sequence += 1
            request_id = self.sequence
            waiter = queue.Queue()
            self.pending[request_id] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": params}, deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            response = waiter.get(timeout=remaining)
            if response is None:
                raise Uncertain("App Server disconnected after submission")
            if "error" in response:
                raise RpcError(response["error"])
            if "result" not in response:
                raise Uncertain("App Server returned a malformed response after submission")
            return response["result"]
        except queue.Empty as exc:
            # A timed-out transport is no longer reused. Reconciliation goes
            # through SessionPool and gets a fresh connection while the stable
            # client message id protects against replay.
            self.close()
            raise Uncertain("App Server response timed out; acceptance unknown") from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        with self._close_lock:
            owner = not self._close_started
            if owner:
                self._close_started = True
                self.closed = True
                with self.lock:
                    for waiter in self.pending.values():
                        waiter.put(None)
                self.outbound.put(None)
        if not owner:
            if threading.current_thread() not in (self.reader, self.writer):
                self._close_done.wait(timeout=4)
            return
        try:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
                except ProcessLookupError:
                    self.proc.wait()
            if self.writer and self.writer is not threading.current_thread():
                self.writer.join(timeout=2)
            if self.reader and self.reader is not threading.current_thread():
                self.reader.join(timeout=2)
            if self.proc:
                for stream in (self.proc.stdin, self.proc.stdout):
                    if stream:
                        try:
                            stream.close()
                        except (BrokenPipeError, OSError, ValueError):
                            pass
        finally:
            self._close_done.set()


class AppServerSession:
    def __init__(self, rpc, *, max_reconcile_pages=100, reconcile_timeout=30, clock=time.monotonic):
        if type(max_reconcile_pages) is not int or max_reconcile_pages <= 0:
            raise ValueError("max_reconcile_pages must be a positive integer")
        if not isinstance(reconcile_timeout, (int, float)) or isinstance(reconcile_timeout, bool) or reconcile_timeout <= 0:
            raise ValueError("reconcile_timeout must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.rpc = rpc
        self.max_reconcile_pages = max_reconcile_pages
        self.reconcile_timeout = reconcile_timeout
        self.clock = clock

    def check_target(self, thread):
        try:
            live = self.rpc.call("thread/loaded/list", {})["data"]
        except Uncertain as exc:
            raise Retryable("cannot verify that the interactive thread is loaded") from exc
        except RpcError as exc:
            raise _operation_error("loaded-thread validation", exc) from exc
        if thread not in live:
            raise Retryable("open this exact thread in the client attached to the same App Server")

    def deliver(self, thread, client_id, text):
        self.check_target(thread)
        try:
            value = self.rpc.call("thread/queue/add", {
                "threadId": thread, "clientUserMessageId": client_id,
                "input": [{"type": "text", "text": text}],
            })
        except RpcError as exc:
            raise _submission_error(exc) from exc
        return {"submission_id": value["queuedSubmission"]["id"]}

    def reconcile(self, thread, client_id):
        result = self.inspect(thread, client_id)
        if result["state"] == "unknown":
            return None
        return {"submission_id": result["submission_id"]}

    def inspect(self, thread, client_id):
        """Read bounded native queue/history evidence without changing it."""
        deadline = self.clock() + self.reconcile_timeout
        self.check_target(thread)
        pages = 0
        for method, state in (
            ("thread/queue/list", "queued"),
            ("thread/turns/list", "consumed"),
        ):
            cursor = None
            seen_cursors = set()
            while True:
                remaining = deadline - self.clock()
                if pages >= self.max_reconcile_pages or remaining <= 0:
                    raise Uncertain("delivery reconciliation scan limit reached; acceptance unknown")
                params = {"threadId": thread, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                if method.endswith("turns/list"):
                    params["itemsView"] = "full"
                try:
                    result = self.rpc.call(method, params, timeout=remaining)
                except RpcError as exc:
                    raise _operation_error("delivery reconciliation", exc) from exc
                pages += 1
                for row in result["data"]:
                    if row.get("clientUserMessageId") == client_id:
                        return {"state": state, "submission_id": row["id"]}
                    for item in row.get("items", []):
                        if item.get("type") == "userMessage" and item.get("clientId") == client_id:
                            return {
                                "state": state,
                                "submission_id": item.get("id", client_id),
                            }
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise Retryable("delivery reconciliation returned a repeated pagination cursor")
                seen_cursors.add(cursor)
        # Absence is not proof that a message was never accepted (e.g. deletion,
        # compaction, history lag). Let the operator resolve, never auto-replay.
        return {
            "state": "unknown",
            "reason": "client message ID was not found in the current queue or bounded history scan",
        }


class SharedLocalSession(AppServerSession):
    """Enqueue through the public API without taking ownership of a UI thread.

    Requires the same Codex home / SQLite home as the local client. The native
    queue watcher wakes only threads already owned by that client. Queued input
    remains durable when the client is closed and is consumed when it reopens.
    """

    def check_target(self, thread):
        try:
            # The server validates persistence, archive state and queue support.
            self.rpc.call("thread/queue/list", {"threadId": thread, "limit": 1})
        except RpcError as exc:
            raise _operation_error("local queue target", exc) from exc
        except Uncertain as exc:
            raise Retryable("cannot verify the shared local queue") from exc


class SessionPool:
    def __init__(self):
        self.sessions = {}

    def __call__(self, endpoint):
        existing = self.sessions.get(endpoint)
        if existing and not existing.rpc.closed:
            return existing
        if existing:
            existing.rpc.close()
        adapter = SharedLocalSession if endpoint == "shared-local" else AppServerSession
        session = adapter(Rpc(endpoint, token=server_token()))
        self.sessions[endpoint] = session
        return session

    def close(self):
        for session in self.sessions.values():
            session.rpc.close()
