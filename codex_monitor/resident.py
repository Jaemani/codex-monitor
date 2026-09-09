"""A bounded owner-side App Server keeper for explicit CLI threads.

This module deliberately owns only the App Server connection and the saved
thread registrations.  It never writes a queue item or starts a model turn.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from collections.abc import Iterable

from .errors import Permanent, Retryable, Uncertain
from .monitor import validate_endpoint
from .session import Rpc, RpcError, _operation_error


_LOADED_METHOD = "thread/loaded/list"
_QUEUE_METHOD = "thread/queue/list"
_RESUME_METHOD = "thread/resume"
_FORBIDDEN_METHODS = frozenset(
    {"thread/start", "turn/start", "thread/queue/add", "turn/interrupt"}
)


class ResidentKeeper:
    """Keep explicit saved threads attached to one owner App Server.

    ``run`` blocks until ``stop_event`` is set.  A successful connection is
    kept for periodic, read-only loaded-thread probes. Each target is resumed
    once per connection when registration succeeds; transient target errors
    retry with bounded per-target backoff while healthy registrations remain
    intact.
    """

    def __init__(
        self,
        endpoint: str,
        threads: Iterable[str],
        *,
        rpc_factory=None,
        clock=time.monotonic,
        health_interval: float = 10.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ):
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("ResidentKeeper requires a nonempty owner endpoint")
        if endpoint == "shared-local":
            raise ValueError(
                "ResidentKeeper cannot use shared-local because it would compete with the owning client"
            )
        if not endpoint.startswith(("ws://", "wss://", "unix:///")):
            raise ValueError(
                "ResidentKeeper requires a shareable ws://, wss://, or unix:/// owner endpoint"
            )
        validate_endpoint(endpoint)
        if isinstance(threads, (str, bytes)):
            raise ValueError("ResidentKeeper requires explicit thread IDs, not one string")
        try:
            values = tuple(threads)
        except TypeError as exc:
            raise ValueError("ResidentKeeper requires a nonempty iterable of explicit thread IDs") from exc
        if not values or any(not isinstance(thread, str) or not thread.strip() for thread in values):
            raise ValueError("ResidentKeeper requires a nonempty set of explicit thread IDs")
        if len(set(values)) != len(values):
            raise ValueError("ResidentKeeper thread IDs must be unique")
        if not callable(rpc_factory or Rpc):
            raise ValueError("rpc_factory must be callable")
        if not callable(clock):
            raise ValueError("clock must be callable")
        for value, label in (
            (health_interval, "health_interval"),
            (backoff_initial, "backoff_initial"),
            (backoff_max, "backoff_max"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{label} must be positive")
        if backoff_initial > backoff_max:
            raise ValueError("backoff_initial must not exceed backoff_max")

        self.endpoint = endpoint
        self.threads = values
        self._rpc_factory = rpc_factory or Rpc
        self._clock = clock
        self._health_interval = float(health_interval)
        self._backoff_initial = float(backoff_initial)
        self._backoff_max = float(backoff_max)
        self._lock = threading.RLock()
        self._rpc = None
        self._connected = False
        self._health_ok = False
        self._last_error = None
        self._connected_at = None
        self._last_probe_at = None
        self._states = {
            thread: {
                "loaded": False,
                "subscribed": False,
                "ready": False,
                "error": None,
            }
            for thread in self.threads
        }

    @staticmethod
    def _error_details(error: Exception) -> dict:
        code = getattr(error, "code", None)
        return {"code": code, "message": str(error)}

    @staticmethod
    def _loaded_ids(result) -> set[str]:
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("thread/loaded/list returned an invalid data payload")
        return {thread for thread in result["data"] if isinstance(thread, str)}

    def _set_connected(self, rpc) -> None:
        now = self._clock()
        with self._lock:
            self._rpc = rpc
            self._connected = True
            self._health_ok = False
            self._connected_at = now
            self._last_probe_at = None
            self._last_error = None
            for state in self._states.values():
                state.update({"loaded": False, "subscribed": False, "ready": False, "error": None})

    def _set_disconnected(self, error: Exception | None = None) -> None:
        with self._lock:
            self._connected = False
            self._health_ok = False
            self._rpc = None
            self._last_error = self._error_details(error) if error else None
            for state in self._states.values():
                state.update({"loaded": False, "subscribed": False, "ready": False})

    def _set_global_error(self, error: Exception) -> None:
        with self._lock:
            self._last_error = self._error_details(error)

    def _update_loaded(self, loaded: set[str]) -> None:
        with self._lock:
            for thread, state in self._states.items():
                state["loaded"] = thread in loaded
                if not state["loaded"]:
                    state["subscribed"] = False
                state["ready"] = bool(
                    self._connected and self._health_ok and state["loaded"] and state["subscribed"]
                )

    @staticmethod
    def _registration_classification(operation: str, error: RpcError):
        """Classify target registration errors without hiding stable failures."""

        classified = _operation_error(operation, error)
        if isinstance(classified, Permanent):
            return "permanent"
        # Some owner versions report a writer/ownership conflict as an
        # internal error instead of a stable invalid-target code.  It cannot
        # be repaired by retrying registration on this connection.
        message = str(error).lower()
        if (
            ("writer" in message and ("conflict" in message or "another" in message))
            or "already has an active writer" in message
            or "already owned" in message
            or "owned by another" in message
        ):
            return "permanent"
        return "transient"

    def _resume_one(self, rpc, thread: str) -> str:
        try:
            rpc.call(_RESUME_METHOD, {"threadId": thread})
        except RpcError as error:
            # A target-specific rejection does not stop other saved threads.
            classification = self._registration_classification("thread resume", error)
            with self._lock:
                state = self._states[thread]
                state.update({"subscribed": False, "ready": False, "error": self._error_details(error)})
            return classification
        except (Retryable, Uncertain):
            raise
        except Exception:
            raise
        with self._lock:
            state = self._states[thread]
            state.update({
                "loaded": True,
                "subscribed": True,
                "ready": bool(self._connected and self._health_ok),
                "error": None,
            })
        return "ok"

    def _probe_queue_one(self, rpc, thread: str) -> str:
        try:
            rpc.call(_QUEUE_METHOD, {"threadId": thread, "limit": 1})
        except RpcError as error:
            # Queue capability is a per-thread compatibility/target result;
            # another explicit thread may still be healthy on this owner.
            classification = self._registration_classification("local queue target", error)
            with self._lock:
                state = self._states[thread]
                state.update({"subscribed": False, "ready": False, "error": self._error_details(error)})
            return classification
        return "ok"

    def _register_one(self, rpc, thread: str, stop_event) -> str:
        with self._lock:
            if self._states[thread]["subscribed"]:
                return "ok"
        result = self._probe_queue_one(rpc, thread)
        if result != "ok" or stop_event.is_set():
            return result
        return self._resume_one(rpc, thread)

    def _attach(self, rpc, stop_event):
        """Probe once, then resume each explicit thread on this connection."""

        retries = {
            thread: {"next_retry": self._clock(), "delay": self._backoff_initial, "terminal": False}
            for thread in self.threads
        }

        try:
            loaded = self._loaded_ids(rpc.call(_LOADED_METHOD, {}))
            self._health_ok = True
        except RpcError as error:
            # Keep the connection alive for a target-specific/server error and
            # still attempt explicit resumes.  A transport exception below
            # tears down the connection and follows the backoff path.
            self._set_global_error(error)
            self._health_ok = False
            loaded = set()
        self._update_loaded(loaded)
        for thread in self.threads:
            if stop_event.is_set():
                break
            result = self._register_one(rpc, thread, stop_event)
            if result == "transient":
                self._schedule_retry(retries[thread])
            elif result == "permanent":
                retries[thread]["terminal"] = True
                retries[thread]["next_retry"] = math.inf
            else:
                retries[thread]["next_retry"] = math.inf
        return retries

    def _schedule_retry(self, retry) -> None:
        delay = retry["delay"]
        retry["next_retry"] = self._clock() + delay
        retry["delay"] = min(self._backoff_max, delay * 2)

    def _retry_targets(self, rpc, stop_event, retries) -> None:
        now = self._clock()
        for thread, retry in retries.items():
            if stop_event.is_set():
                return
            with self._lock:
                state = self._states[thread]
                subscribed = state["subscribed"]
            if subscribed:
                retry["next_retry"] = math.inf
                continue
            if retry["terminal"]:
                continue
            # A successful health probe can show that a previously healthy
            # registration disappeared. Reattach that target only; healthy
            # siblings keep their existing subscription.
            if math.isinf(retry["next_retry"]):
                retry["next_retry"] = now
            if retry["next_retry"] > now:
                continue
            result = self._register_one(rpc, thread, stop_event)
            if result == "transient":
                self._schedule_retry(retry)
            elif result == "permanent":
                retry["terminal"] = True
                retry["next_retry"] = math.inf
            elif result == "ok":
                retry["next_retry"] = math.inf

    def _probe(self, rpc) -> bool:
        try:
            loaded = self._loaded_ids(rpc.call(_LOADED_METHOD, {}))
        except RpcError as error:
            self._set_global_error(error)
            with self._lock:
                self._health_ok = False
                for state in self._states.values():
                    state["ready"] = False
            return False
        with self._lock:
            self._health_ok = True
            self._last_probe_at = self._clock()
            self._last_error = None
        self._update_loaded(loaded)
        return True

    def _close(self, rpc) -> None:
        if rpc is None:
            return
        close = getattr(rpc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _connected_loop(self, rpc, stop_event) -> None:
        retries = self._attach(rpc, stop_event)
        while not stop_event.wait(self._health_interval):
            self._probe(rpc)
            self._retry_targets(rpc, stop_event, retries)

    def run(self, stop_event):
        """Run until ``stop_event`` is set and return the final status."""

        if not hasattr(stop_event, "wait") or not hasattr(stop_event, "is_set"):
            raise ValueError("stop_event must provide wait() and is_set()")
        backoff = self._backoff_initial
        while not stop_event.is_set():
            rpc = None
            try:
                rpc = self._rpc_factory(self.endpoint)
                self._set_connected(rpc)
                self._connected_loop(rpc, stop_event)
            except Permanent as error:
                self._set_disconnected(error)
                raise
            except Exception as error:
                if stop_event.is_set():
                    break
                if self._last_probe_at is not None:
                    backoff = self._backoff_initial
                self._last_probe_at = None
                self._set_disconnected(error)
                self._close(rpc)
                rpc = None
                if stop_event.wait(backoff):
                    break
                backoff = min(self._backoff_max, backoff * 2)
            finally:
                self._close(rpc)
                if rpc is not None and rpc is self._rpc:
                    self._set_disconnected()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            subscribed = bool(self._states) and all(
                state["subscribed"] for state in self._states.values()
            )
            return {
                "endpoint": self.endpoint,
                "connected": self._connected,
                "subscribed": subscribed,
                "threads": copy.deepcopy(self._states),
                "last_error": copy.deepcopy(self._last_error),
                "connected_at": self._connected_at,
                "last_probe_at": self._last_probe_at,
                "protocol": {
                    "server_requests": "awaiting_native_interactive_handling",
                    "approval_policy": "no_autoanswer",
                    "integration_needed": (
                        "The keeper sends no response to server-initiated approval/tool requests; Rpc/native "
                        "interactive handling owns them."
                    ),
                    "forbidden_methods": sorted(_FORBIDDEN_METHODS),
                },
            }
