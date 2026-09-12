"""Bounded, read-only health probes for an existing Codex App Server owner.

The dashboard needs to answer a narrow operational question: can an existing
owner currently receive work for one saved conversation?  This module uses
only explicit WebSocket transports.  It never creates a stdio App Server,
starts or resumes a thread, writes a queue item, or asks a model to run.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import math
import time
from typing import Any, Callable
from urllib.parse import urlsplit


DEFAULT_TIMEOUT = 0.45
DEFAULT_CACHE_TTL = 2.0
DEFAULT_CACHE_SIZE = 64
MAX_PAGES = 8
MAX_MESSAGE_BYTES = 256 * 1024
MAX_THREAD_IDS = 4096

_AUTH_WORDS = (
    "auth", "credential", "forbidden", "invalid token", "logged out",
    "login", "log in", "not authenticated", "unauthorized", "unauthorised",
)
_UNAVAILABLE_WORDS = (
    "unavailable", "connection", "connect", "closed", "disconnect",
    "refused", "timed out", "timeout", "network", "unreachable",
)


class OwnerProbeError(Exception):
    """An internal bounded probe failure; its message is never user output."""

    def __init__(self, kind: str, method: str | None = None):
        self.kind = kind
        self.method = method
        super().__init__(kind)


class _RpcError(Exception):
    def __init__(self, code: Any, message: Any):
        self.code = code
        self.message = str(message or "RPC error")
        super().__init__(self.message)


def _supported_endpoint(endpoint: Any) -> bool:
    if not isinstance(endpoint, str) or not endpoint or any(ord(char) < 32 for char in endpoint):
        return False
    if endpoint.startswith("unix://"):
        path = endpoint.removeprefix("unix://")
        return path.startswith("/") and len(path) > 1 and "\x00" not in path
    if not endpoint.startswith(("ws://", "wss://")):
        return False
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"ws", "wss"} or not parsed.netloc or not host or
        parsed.username or parsed.password or parsed.fragment or
        any(char.isspace() for char in endpoint) or parsed.netloc.endswith(":")
    ):
        return False
    return parsed.scheme == "wss" or host.lower() in {"127.0.0.1", "localhost", "::1"}


def _display_endpoint(endpoint: Any) -> str:
    """Keep credentials out of probe observations, including rejected URLs."""

    if not isinstance(endpoint, str):
        return "-"
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return "owner endpoint"
    if parsed.username or parsed.password:
        host = parsed.hostname or "owner"
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        port = f":{parsed_port}" if parsed_port is not None else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    return endpoint[:240]


def _auth_error(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if code in {401, 403, -32001, -32002}:
        return True
    text = str(error).casefold()
    return any(word in text for word in _AUTH_WORDS) or "http 401" in text or "http 403" in text


def _unsupported(error: BaseException) -> bool:
    if getattr(error, "code", None) == -32601:
        return True
    text = str(error).casefold()
    return "method not found" in text or "unknown variant" in text or "unsupported method" in text


def _status_value(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        if value and len(value) <= 64:
            return "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value)
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            result = _status_value(value.get(key))
            if result:
                return result
    return None


class _OwnerRpc:
    """One short-lived WebSocket RPC connection."""

    def __init__(self, endpoint: str, *, timeout: float, token: str | None):
        try:
            from websockets.sync.client import connect, unix_connect
        except Exception as exc:  # pragma: no cover - dependency is declared
            raise OwnerProbeError("unavailable") from exc
        try:
            if endpoint.startswith("unix://"):
                self._socket = unix_connect(
                    endpoint.removeprefix("unix://"),
                    open_timeout=timeout,
                    close_timeout=min(timeout, 0.5),
                    compression=None,
                    max_size=MAX_MESSAGE_BYTES,
                )
            else:
                headers = {"Authorization": "Bearer " + token} if token else None
                self._socket = connect(
                    endpoint,
                    additional_headers=headers,
                    open_timeout=timeout,
                    close_timeout=min(timeout, 0.5),
                    compression=None,
                    max_size=MAX_MESSAGE_BYTES,
                )
        except Exception as exc:
            raise OwnerProbeError("auth-required" if _auth_error(exc) else "unavailable") from exc
        self._next_id = 0

    def call(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        try:
            encoded = json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise OwnerProbeError("unavailable", method)
            self._socket.send(encoded)
            deadline = time.monotonic() + max(0.01, timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerProbeError("unavailable", method)
                raw = self._socket.recv(timeout=remaining)
                if raw is None:
                    raise OwnerProbeError("unavailable", method)
                if isinstance(raw, bytes):
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise OwnerProbeError("unavailable", method)
                    raw = raw.decode("utf-8")
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    raise OwnerProbeError("unavailable", method)
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise OwnerProbeError("unavailable", method) from exc
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if "error" in message and isinstance(message["error"], dict):
                    error = message["error"]
                    raise _RpcError(error.get("code"), error.get("message"))
                if "result" not in message:
                    raise OwnerProbeError("unavailable", method)
                return message["result"]
        except OwnerProbeError:
            raise
        except _RpcError:
            raise
        except Exception as exc:
            raise OwnerProbeError("auth-required" if _auth_error(exc) else "unavailable", method) from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._socket.send(json.dumps({"method": method, "params": params}, separators=(",", ":")))
        except Exception as exc:
            raise OwnerProbeError("unavailable", method) from exc

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass


def _page(rpc: _OwnerRpc, method: str, *, timeout: float) -> list[Any]:
    rows: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
        result = rpc.call(method, params, timeout=timeout)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise OwnerProbeError("unavailable", method)
        rows.extend(result["data"])
        if len(rows) > MAX_THREAD_IDS:
            raise OwnerProbeError("unavailable", method)
        next_cursor = result.get("nextCursor")
        if not next_cursor:
            return rows
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise OwnerProbeError("unavailable", method)
        seen.add(next_cursor)
        cursor = next_cursor
    raise OwnerProbeError("unavailable", method)


def _account_observation(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "unknown",
            "present": None,
            "credential_validation": "unverified",
            "refresh_token": False,
        }
    account = result.get("account") if "account" in result else None
    requires_auth = result.get("requiresOpenaiAuth")
    if account is None and requires_auth is True:
        return {
            "status": "auth-required",
            "present": False,
            "credential_validation": "unverified",
            "refresh_token": False,
        }
    if account is None and requires_auth is not False:
        status = "unknown"
    else:
        status = "present" if account else "absent"
    # Keep this a presence observation.  An account object is not evidence
    # that a credential will authorize a later operation.
    return {
        "status": status,
        "present": bool(account) if status in {"present", "absent"} else None,
        "credential_validation": "unverified",
        "refresh_token": False,
    }


def _base(endpoint: str, thread: str) -> dict[str, Any]:
    return {
        "endpoint": _display_endpoint(endpoint),
        "thread": thread,
        "status": "unverified",
        "state": "unverified",
        "ready": False,
        "transport_reachable": False,
        "thread_loaded": None,
        "account": {"status": "unverified", "present": None, "credential_validation": "unverified"},
        "credential_validation": "unverified",
        "model_execution": "unverified",
        "reason": None,
    }


def probe_owner(
    endpoint: str,
    thread: str,
    *,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    rpc_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Probe one existing owner using only bounded, read-only RPC methods."""

    result = _base(endpoint, thread)
    if endpoint in {"shared-local", "local"}:
        result["reason"] = "owner is not explicit; readiness is unverified"
        return result
    if not _supported_endpoint(endpoint):
        result["reason"] = "endpoint is not a supported explicit WebSocket owner"
        return result
    if not isinstance(thread, str) or not thread:
        result["reason"] = "conversation identity is unavailable"
        return result
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        timeout = DEFAULT_TIMEOUT
    timeout = max(0.05, min(float(timeout), DEFAULT_TIMEOUT))
    rpc: Any = None
    try:
        rpc = (rpc_factory or _OwnerRpc)(endpoint, timeout=timeout, token=token)
        rpc.call(
            "initialize",
            {"clientInfo": {"name": "codex-monitor-dashboard", "title": "Codex Monitor Dashboard", "version": "0.1.0"},
             "capabilities": {"experimentalApi": True}},
            timeout=timeout,
        )
        rpc.notify("initialized", {})
        result["transport_reachable"] = True

        try:
            account = rpc.call("account/read", {"refreshToken": False}, timeout=timeout)
        except _RpcError as exc:
            if _unsupported(exc):
                result["account"] = {"status": "unsupported", "present": None,
                                      "credential_validation": "unverified", "refresh_token": False}
            elif _auth_error(exc):
                result.update(status="auth-required", state="auth-required", reason="authentication required")
                return result
            else:
                result["account"] = {"status": "unavailable", "present": None,
                                      "credential_validation": "unverified", "refresh_token": False}
        else:
            result["account"] = _account_observation(account)
            if result["account"].get("status") == "auth-required":
                result.update(status="auth-required", state="auth-required", reason="authentication required")
                return result

        try:
            loaded = _page(rpc, "thread/loaded/list", timeout=timeout)
        except _RpcError as exc:
            if _auth_error(exc):
                result.update(status="auth-required", state="auth-required", reason="authentication required")
            else:
                result["reason"] = "loaded conversations could not be read"
            return result
        except OwnerProbeError:
            result["reason"] = "loaded conversations could not be read"
            return result
        loaded_ids = {row if isinstance(row, str) else row.get("id") for row in loaded if isinstance(row, (str, dict))}
        loaded_ids.discard(None)
        result["loaded_threads"] = sorted(loaded_ids)[:MAX_THREAD_IDS]
        result["thread_loaded"] = thread in loaded_ids
        if thread not in loaded_ids:
            result.update(status="unloaded", state="unloaded", reason="conversation is not loaded by this owner")
            return result

        try:
            thread_read = rpc.call(
                "thread/read", {"threadId": thread, "includeTurns": False}, timeout=timeout
            )
        except _RpcError as exc:
            if _auth_error(exc):
                result.update(status="auth-required", state="auth-required", reason="authentication required")
                return result
            if not _unsupported(exc):
                result.update(status="unavailable", state="unavailable", reason="thread status unavailable")
                return result
            # Older peers may lack thread/read, so retain the loaded-list
            # readiness observation with an explicit unsupported note.
            result["thread_read"] = {"status": "unsupported"}
        except OwnerProbeError:
            result.update(status="unavailable", state="unavailable", reason="thread status unavailable")
            return result
        else:
            status = None
            if isinstance(thread_read, dict):
                status = _status_value(thread_read.get("status"))
                if status is None and isinstance(thread_read.get("thread"), dict):
                    status = _status_value(thread_read["thread"].get("status"))
            result["thread_read"] = {"status": status or "observed"}
        result.update(status="ready-to-receive", state="ready-to-receive", ready=True,
                      reason="owner transport reachable and conversation loaded")
        return result
    except _RpcError as exc:
        result.update(status="auth-required" if _auth_error(exc) else "unavailable",
                      state="auth-required" if _auth_error(exc) else "unavailable",
                      reason="authentication required" if _auth_error(exc) else "owner RPC unavailable")
        return result
    except OwnerProbeError as exc:
        result.update(status=exc.kind, state=exc.kind,
                      reason="authentication required" if exc.kind == "auth-required" else "owner unavailable")
        return result
    except Exception:
        result.update(status="unavailable", state="unavailable", reason="owner unavailable")
        return result
    finally:
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass


class OwnerHealthCache:
    """Small TTL cache preventing a live dashboard from reconnecting per frame."""

    def __init__(self, *, probe: Callable[..., dict[str, Any]] = probe_owner,
                 ttl: float = DEFAULT_CACHE_TTL, max_entries: int = DEFAULT_CACHE_SIZE,
                 clock: Callable[[], float] = time.monotonic):
        if not callable(probe):
            raise ValueError("owner probe must be callable")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("owner health cache ttl must be positive")
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("owner health cache size must be positive")
        self.probe = probe
        self.ttl = float(ttl)
        self.max_entries = max_entries
        self.clock = clock
        self._items: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, endpoint: str, thread: str) -> dict[str, Any]:
        key = (endpoint, thread)
        now = self.clock()
        cached = self._items.get(key)
        if cached and cached[0] > now:
            self._items.move_to_end(key)
            return dict(cached[1])
        value = self.probe(endpoint, thread)
        if not isinstance(value, dict):
            value = _base(endpoint, thread)
            value.update(status="unavailable", state="unavailable", reason="owner probe returned invalid data")
        self._items[key] = (now + self.ttl, dict(value))
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)
        return dict(value)
