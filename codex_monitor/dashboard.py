"""Read-only terminal inventory for the local codex-monitor state.

The dashboard deliberately does not use :class:`Monitor` or any of the
durable stores.  Their constructors create and migrate databases, which is
the wrong thing for a view-only command.  This module uses SQLite's URI
``mode=ro`` and a small busy timeout instead.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import select
import shutil
import sqlite3
import sys
import threading
import time
import re
import stat as stat_module
import unicodedata
from datetime import datetime
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


READ_TIMEOUT = 0.35
STATUS_TIMEOUT = 0.75
STATUS_BODY_LIMIT = 64 * 1024
CHECKPOINT_LIMIT = 64 * 1024
MAX_TEXT = 240


class DashboardError(RuntimeError):
    """A state inventory could not be read safely."""


def _safe(value: Any, limit: int = MAX_TEXT) -> str:
    """Return one terminal-safe, bounded line fragment.

    State values are producer controlled in several places (event ids,
    paths, error strings).  Removing every C0/C1 character, including ESC,
    keeps those values from changing terminal state when rendered.
    """

    if value is None:
        return "-"
    text = str(value)
    text = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} or 0x7F <= ord(char) <= 0x9F else char
        for char in text
    )
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _age(value: Any, now: float) -> float | None:
    timestamp = _number(value)
    if timestamp is None:
        return None
    return max(0.0, now - timestamp)


def _age_text(value: Any) -> str:
    age = _number(value)
    if age is None:
        return "unknown"
    if age < 1:
        return "<1s"
    if age < 60:
        return f"{age:.0f}s"
    if age < 3600:
        return f"{age / 60:.1f}m"
    if age < 86400:
        return f"{age / 3600:.1f}h"
    return f"{age / 86400:.1f}d"


def _read_limited(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise DashboardError(f"{path.name} is not a regular file")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = -1
            value = stream.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(value) > limit:
        raise DashboardError(f"{path.name} exceeds the {limit} byte read limit")
    return value


def _read_json(path: Path, limit: int) -> Any:
    try:
        return json.loads(_read_limited(path, limit))
    except DashboardError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise DashboardError(f"cannot read {path.name}: {type(exc).__name__}") from exc


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without allowing writes."""

    if not path.is_file():
        raise DashboardError(f"database is missing: {path.name}")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=350")
        return db
    except (OSError, sqlite3.Error) as exc:
        raise DashboardError(f"cannot open {path.name} read-only: {type(exc).__name__}") from exc


def _tables(db: sqlite3.Connection) -> set[str]:
    try:
        return {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
    except sqlite3.Error as exc:
        raise DashboardError(f"database schema is corrupt or unreadable ({type(exc).__name__})") from exc


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as exc:
        raise DashboardError(f"cannot inspect {table} schema: {type(exc).__name__}") from exc


def _parse_sources(value: Any) -> tuple[list[str], str | None]:
    try:
        sources = json.loads(value)
        if not isinstance(sources, list):
            raise ValueError("sources is not a list")
        return [_safe(source) for source in sources], None
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        return [], f"invalid sources JSON ({type(exc).__name__})"


def _last_observation(checkpoint: Any) -> dict[str, Any] | None:
    if not isinstance(checkpoint, dict):
        return None
    value = checkpoint.get("last")
    if not isinstance(value, dict):
        return None
    fields = ("path", "state", "sha256", "error", "predicate")
    return {key: _safe(value[key]) for key in fields if key in value}


def _checkpoint(root: Path, watch_id: str, now: float) -> dict[str, Any]:
    # The filename is generated by Monitor and is not taken from a producer.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", watch_id):
        return {
            "present": False,
            "age_seconds": None,
            "last_observation": None,
            "pending": None,
            "error": "invalid collector id in state database",
        }
    path = root / "managed" / (watch_id + ".json")
    result: dict[str, Any] = {
        "present": False,
        "age_seconds": None,
        "last_observation": None,
        "pending": None,
        "error": None,
    }
    try:
        stat = path.stat()
        result["present"] = True
        result["age_seconds"] = _age(stat.st_mtime, now)
        value = _read_json(path, CHECKPOINT_LIMIT)
        if not isinstance(value, dict) or "last" not in value or "pending" not in value:
            raise DashboardError("checkpoint must contain last and pending")
        result["last_observation"] = _last_observation(value)
        result["pending"] = bool(value.get("pending"))
    except FileNotFoundError:
        pass
    except DashboardError as exc:
        result["error"] = _safe(str(exc))
    except OSError as exc:
        result["error"] = _safe(f"checkpoint unavailable ({type(exc).__name__})")
    return result


def _connection(thread: str) -> dict[str, Any]:
    return {
        "thread": _safe(thread),
        "bindings": [],
        "collectors": [],
        "events": {"counts": {}, "latest": None},
        "requests": {"counts": {}, "latest": None},
    }


def _latest_event(db: sqlite3.Connection, binding: str, now: float) -> dict[str, Any] | None:
    row = db.execute(
        """SELECT id,event_id,source,state,created,updated,attempts,submission_id,error
           FROM events WHERE binding=? ORDER BY seq DESC LIMIT 1""", (binding,)
    ).fetchone()
    if row is None:
        return None
    return {
        "delivery_id": _safe(row["id"]),
        "event_id": _safe(row["event_id"]),
        "source": _safe(row["source"]),
        "state": _safe(row["state"]),
        "created": row["created"],
        "updated": row["updated"],
        "age_seconds": _age(row["created"], now),
        "attempts": row["attempts"],
        "submission_id": _safe(row["submission_id"]) if row["submission_id"] is not None else None,
        "error": _safe(row["error"]) if row["error"] else None,
    }


def _request_inventory(root: Path, thread: str, now: float, deadline: float | None = None) -> dict[str, Any]:
    path = root / "requests.sqlite3"
    if not path.is_file():
        return {"available": False, "reason": "request store is not initialized", "counts": {}, "latest": None}
    try:
        db = _ro_connect(path)
    except DashboardError as exc:
        return {"available": False, "reason": _safe(str(exc)), "counts": {}, "latest": None}
    try:
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        tables = _tables(db)
        if "requests" not in tables:
            return {"available": False, "reason": "request store is missing the requests table", "counts": {}, "latest": None}
        counts = {
            _safe(row["state"]): row["n"] for row in db.execute(
                "SELECT state,count(*) n FROM requests WHERE conversation_id=? GROUP BY state", (thread,)
            )
        }
        row = db.execute(
            """SELECT request_id,source,request_key,state,revision,created,updated,expires_at
               FROM requests WHERE conversation_id=? ORDER BY seq DESC LIMIT 1""", (thread,)
        ).fetchone()
        latest = None
        if row is not None:
            latest = {
                "request_id": _safe(row["request_id"]),
                "source": _safe(row["source"]),
                "request_key": _safe(row["request_key"]),
                "state": _safe(row["state"]),
                "revision": row["revision"],
                "created": row["created"],
                "updated": row["updated"],
                "age_seconds": _age(row["updated"], now),
                "expires_at": row["expires_at"],
            }
        return {"available": True, "counts": counts, "latest": latest}
    except sqlite3.Error as exc:
        return {"available": False, "reason": _safe(f"request store read failed ({type(exc).__name__})"), "counts": {}, "latest": None}
    finally:
        db.close()


def _receiver_lock_probe(path: Path) -> bool | None:
    """Return lock state, or ``None`` when the read-only probe is inconclusive."""
    try:
        if not stat_module.S_ISREG(path.lstat().st_mode):
            return False
    except OSError:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                return False
            stream = os.fdopen(fd, "rb", closefd=True)
            fd = -1
        finally:
            if fd >= 0:
                os.close(fd)
        with stream:
            if os.name == "nt":
                # The project lock uses msvcrt on Windows; a read-only probe
                # cannot safely acquire that byte lock.  Existence remains a
                # useful distinct observation on that platform.
                return True
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            finally:
                try:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                except OSError:
                    pass
            return False
    except OSError:
        return None


def receiver_process_alive(path: Path) -> bool:
    """Check the existing receiver lock without creating or changing it."""

    return _receiver_lock_probe(path) is True


class DashboardReader:
    """Build one bounded, read-only state inventory."""

    def __init__(self, root: str | os.PathLike[str], *, thread: str | None = None, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.thread = thread
        self.clock = clock

    def _monitor_inventory(self, now: float, deadline: float | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        db = _ro_connect(self.root / "monitor.sqlite3")
        try:
            if deadline is not None:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            # Keep bindings, counts and latest receipts from one SQLite
            # snapshot while the receiver continues writing in another
            # connection.
            db.execute("BEGIN")
            tables = _tables(db)
            required = {"bindings", "events"}
            missing = sorted(required - tables)
            if missing:
                raise DashboardError("monitor database is missing table(s): " + ", ".join(missing))
            binding_columns = _columns(db, "bindings")
            managed_columns = _columns(db, "managed_watches") if "managed_watches" in tables else set()
            connections: dict[str, dict[str, Any]] = {}
            binding_query = "SELECT name,thread,endpoint,sources" + (
                ",enabled" if "enabled" in binding_columns else ""
            ) + " FROM bindings ORDER BY thread,name"
            for row in db.execute(binding_query):
                source_list, source_error = _parse_sources(row["sources"])
                item = {
                    "name": _safe(row["name"]),
                    "thread": _safe(row["thread"]),
                    "endpoint": _safe(row["endpoint"]),
                    "sources": source_list,
                    "enabled": bool(row["enabled"]) if "enabled" in binding_columns else None,
                    "schema_error": source_error,
                    "events": {"counts": {}, "latest": None},
                }
                if self.thread is not None and row["thread"] != self.thread:
                    continue
                value = connections.setdefault(row["thread"], _connection(row["thread"]))
                value["bindings"].append(item)
                counts = {
                    _safe(event["state"]): event["n"] for event in db.execute(
                        "SELECT state,count(*) n FROM events WHERE binding=? GROUP BY state", (row["name"],)
                    )
                }
                latest = _latest_event(db, row["name"], now)
                item["events"] = {"counts": counts, "latest": latest}
                for state, count in counts.items():
                    value["events"]["counts"][state] = value["events"]["counts"].get(state, 0) + count
                if latest and (
                    value["events"]["latest"] is None
                    or (latest.get("created") or 0) > (value["events"]["latest"].get("created") or 0)
                ):
                    value["events"]["latest"] = latest

            if managed_columns:
                wanted = [
                    "id", "thread", "name", "path", "interval", "binding", "enabled", "worker_state",
                    "last_error", "last_sample_error", "last_delivery_id", "worker_seen", "updated",
                ]
                selected = [name for name in wanted if name in managed_columns]
                for row in db.execute(
                    "SELECT " + ",".join(selected) + " FROM managed_watches WHERE removed=0 ORDER BY thread,name"
                ):
                    if self.thread is not None and row["thread"] != self.thread:
                        continue
                    value = connections.setdefault(row["thread"], _connection(row["thread"]))
                    checkpoint = _checkpoint(self.root, str(row["id"]), now)
                    worker_age = _age(row["worker_seen"], now) if "worker_seen" in managed_columns else None
                    value["collectors"].append({
                        "id": _safe(row["id"]),
                        "name": _safe(row["name"]),
                        "binding": _safe(row["binding"]),
                        "path": _safe(row["path"]),
                        "interval_seconds": row["interval"],
                        "enabled": bool(row["enabled"]) if "enabled" in managed_columns else None,
                        # These are persisted observations.  The dashboard
                        # intentionally does not call them proof of activity.
                        "worker_state": _safe(row["worker_state"]) if "worker_state" in managed_columns else None,
                        "worker_seen_age_seconds": worker_age,
                        "worker_seen_status": (
                            "fresh" if worker_age is not None and worker_age <= 2.0 else
                            "stale" if worker_age is not None else "unknown"
                        ),
                        "updated_age_seconds": _age(row["updated"], now) if "updated" in managed_columns else None,
                        "last_delivery_id": _safe(row["last_delivery_id"]) if row["last_delivery_id"] else None,
                        "last_error": _safe(row["last_error"]) if row["last_error"] else None,
                        "last_sample_error": _safe(row["last_sample_error"]) if row["last_sample_error"] else None,
                        "checkpoint": checkpoint,
                        "activity": "unknown",
                    })
            result = [connections[key] for key in sorted(connections, key=lambda value: _safe(value))]
            for value in result:
                value["requests"] = _request_inventory(self.root, value["thread"], now, deadline)
            return result, []
        except sqlite3.Error as exc:
            raise DashboardError(f"monitor database is corrupt or unreadable ({type(exc).__name__})") from exc
        finally:
            db.close()

    def _receiver(self) -> dict[str, Any]:
        lock_path = self.root / "serve.lock"
        probe = _receiver_lock_probe(lock_path)
        process = probe is True
        result: dict[str, Any] = {
            "process_alive": None if probe is None else process,
            "status_checked": False,
            "ready": False,
            "reason": "receiver lock status is unknown" if probe is None else ("receiver lock is not held" if not process else None),
        }
        if probe is not True:
            return result
        config_path = self.root / "config.json"
        try:
            config = _read_json(config_path, 64 * 1024)
            port = config.get("port") if isinstance(config, dict) else None
            if type(port) is not int or not 1 <= port <= 65535:
                raise DashboardError("receiver port is invalid")
            token_path = self.root / "admin.token"
            token = _read_limited(token_path, 4096).decode("utf-8").strip()
            if not token:
                raise DashboardError("admin token is empty")
            url = f"http://127.0.0.1:{port}/v1/status"
            request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})

            class _NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            opener = build_opener(_NoRedirect, ProxyHandler({}))
            with opener.open(request, timeout=STATUS_TIMEOUT) as response:
                if response.status != 200:
                    raise DashboardError(f"status endpoint returned HTTP {response.status}")
                deadline = time.monotonic() + STATUS_TIMEOUT
                body_buffer = bytearray()
                while len(body_buffer) <= STATUS_BODY_LIMIT:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DashboardError("status endpoint timed out")
                    # urllib's socket timeout is per read.  Tighten it to
                    # the remaining overall probe budget when the standard
                    # HTTP response exposes its socket.
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read(min(8192, STATUS_BODY_LIMIT + 1 - len(body_buffer)))
                    if not chunk:
                        break
                    body_buffer.extend(chunk)
                body = bytes(body_buffer)
            if len(body) > STATUS_BODY_LIMIT:
                raise DashboardError("status endpoint response exceeds the read limit")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise DashboardError("status endpoint returned a non-object")
            capabilities = payload.get("capabilities")
            if not isinstance(payload.get("bindings"), list) or not isinstance(payload.get("events"), dict):
                raise DashboardError("status endpoint returned an invalid inventory")
            if not isinstance(capabilities, dict):
                raise DashboardError("status endpoint omitted capabilities")
            degraded = bool(payload.get("worker_error"))
            result.update({
                "status_checked": True,
                "ready": True,
                "health": "degraded" if degraded else "ready",
                "capabilities": {
                    _safe(key): bool(value) for key, value in capabilities.items()
                },
            })
        except HTTPError as exc:
            result["status_checked"] = True
            try:
                result["reason"] = f"status endpoint returned HTTP {exc.code}"
            finally:
                exc.close()
        except (DashboardError, OSError, URLError, TimeoutError, ValueError, TypeError) as exc:
            result["status_checked"] = True
            result["reason"] = _safe(f"status probe failed ({type(exc).__name__})")
        return result

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        if _number(now) is None:
            now = time.time()
        try:
            connections, warnings = self._monitor_inventory(now, time.monotonic() + READ_TIMEOUT)
        except DashboardError as exc:
            return {
                "ok": False,
                "read_only": True,
                "generated_at": now,
                "error": _safe(str(exc)),
                "receiver": self._receiver(),
                "connections": [],
            }
        return {
            "ok": True,
            "read_only": True,
            "generated_at": now,
            "thread_filter": _safe(self.thread) if self.thread is not None else None,
            "receiver": self._receiver(),
            "connections": connections,
            "warnings": warnings,
            "note": "Source health, worker activity and model activity are unknown; values shown are persisted observations and receipts.",
        }


_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _cell_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _clip(text: str, width: int) -> str:
    """Clip a terminal line by display cells, retaining only our SGR codes."""

    if width <= 0:
        return ""
    # Preserve only the small SGR vocabulary emitted by this module; every
    # other control character is sanitized as ordinary producer text.
    raw = str(text)
    sanitized: list[str] = []
    position = 0
    while position < len(raw):
        match = _ANSI_SGR.match(raw, position)
        if match:
            sanitized.append(match.group(0))
            position = match.end()
        else:
            sanitized.append(_safe(raw[position], 1))
            position += 1
    text = "".join(sanitized)
    def char_width(char: str) -> int:
        return _cell_width(char)

    visible = sum(char_width(char) for char in _ANSI_SGR.sub("", text))
    if visible <= width:
        return text
    result: list[str] = []
    used = 0
    position = 0
    while position < len(text):
        match = _ANSI_SGR.match(text, position)
        if match:
            result.append(match.group(0))
            position = match.end()
            continue
        char = text[position]
        size = char_width(char)
        if used + size + 1 > width:
            break
        result.append(char)
        used += size
        position += 1
    clipped = "".join(result)
    return clipped + ("…\x1b[0m" if "\x1b[" in clipped else "…")


def _paint(text: Any, code: int | None, color: bool) -> str:
    value = _safe(text)
    return f"\x1b[{code}m{value}\x1b[0m" if color and code is not None else value


def _status(value: str, color: bool) -> str:
    codes = {"ON": 32, "OFF": 31, "STALE": 33, "UNKNOWN": 33}
    return _paint(value, codes.get(value), color)


def _status_cell(value: str, color: bool, width: int = 8) -> str:
    """Render a status label with stable display-cell width in the table."""

    return _status(value, color) + " " * max(0, width - len(value))


def _receiver_status(receiver: dict[str, Any]) -> str:
    if receiver.get("process_alive") is None:
        return "UNKNOWN"
    if receiver.get("process_alive") is False:
        return "OFF"
    if receiver.get("ready") and receiver.get("health") != "degraded":
        return "ON"
    return "STALE" if receiver.get("status_checked") else "UNKNOWN"


def _binding_status(binding: dict[str, Any]) -> str:
    enabled = binding.get("enabled")
    if enabled is True:
        return "ON"
    if enabled is False:
        return "OFF"
    return "UNKNOWN"


def _collector_status(collector: dict[str, Any]) -> str:
    if collector.get("enabled") is False:
        return "OFF"
    if collector.get("last_error") or collector.get("last_sample_error") or collector.get("checkpoint", {}).get("error"):
        return "STALE"
    if collector.get("worker_seen_status") == "stale":
        return "STALE"
    if collector.get("enabled") is True and collector.get("worker_seen_status") == "fresh":
        return "ON"
    return "UNKNOWN"


def _fit(text: Any, width: int) -> str:
    """Return a fixed-width plain table cell without padding untrusted ANSI."""

    value = _clip(_safe(text), max(0, width))
    visible = sum(_cell_width(char) for char in value)
    return value + " " * max(0, width - visible)


def _event_summary(value: dict[str, Any]) -> str:
    counts = value.get("counts") or {}
    rendered = ", ".join(f"{_safe(key)}={count}" for key, count in sorted(counts.items())) or "none"
    latest = value.get("latest")
    if latest:
        identifier = latest.get("delivery_id") or latest.get("request_id")
        rendered += f"; latest {_safe(latest.get('state'))} {_safe(identifier)} ({_age_text(latest.get('age_seconds'))})"
    return rendered


def _event_compact(value: dict[str, Any]) -> str:
    """Summarize counts and age without putting receipt UUIDs in the table."""

    counts = value.get("counts") or {}
    rendered = ", ".join(f"{_safe(key)}={count}" for key, count in sorted(counts.items())) or "none"
    latest = value.get("latest")
    if latest:
        rendered += f"; {_safe(latest.get('state'))} {_age_text(latest.get('age_seconds'))}"
    return rendered


def _binding_rows(snapshot: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    rows: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    index = 0
    for connection in snapshot.get("connections") or []:
        for binding in connection.get("bindings") or []:
            rows.append((connection, binding, index))
            index += 1
    return rows


def _refresh_line(snapshot: dict[str, Any], now: float) -> str:
    timestamp = _number(snapshot.get("generated_at"))
    if timestamp is None:
        return "Last refreshed: unknown (age unknown)"
    refreshed = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    age = max(0.0, now - timestamp)
    return f"Last refreshed: {refreshed} (age {_age_text(age)})"


def _detail_lines(connection: dict[str, Any], binding: dict[str, Any], color: bool) -> list[str]:
    """Render details only after the user explicitly asks for them."""

    lines = [f"  Details: {_safe(binding.get('name'))} in {_safe(connection.get('thread'))}"]
    lines.append(
        f"    status {_status(_binding_status(binding), color)} · endpoint {_safe(binding.get('endpoint'))} · "
        f"sources {', '.join(_safe(source) for source in binding.get('sources', [])) or 'none'}"
    )
    if binding.get("schema_error"):
        lines.append("    schema error: " + _safe(binding["schema_error"]))
    lines.append("    events: " + _event_summary(binding.get("events", {})))
    requests = connection.get("requests") or {}
    lines.append("    requests: " + (_event_summary(requests) if requests.get("available") else _safe(requests.get("reason", "unavailable"))))
    collectors = [item for item in connection.get("collectors", []) if item.get("binding") == binding.get("name")]
    for collector in collectors:
        observation = collector.get("checkpoint", {}).get("last_observation") or {}
        observed = _safe(observation.get("state"), 40) if observation else "none"
        lines.append(
            f"    collector {_safe(collector.get('name'))}: {_status(_collector_status(collector), color)}; "
            f"seen {_age_text(collector.get('worker_seen_age_seconds'))}/{_safe(collector.get('worker_seen_status'))}; sample={observed}"
        )
        errors = [collector.get("last_error"), collector.get("last_sample_error"), collector.get("checkpoint", {}).get("error")]
        errors = [_safe(error) for error in errors if error]
        if errors:
            lines.append("      error: " + "; ".join(errors))
    return lines


def render_lines(snapshot: dict[str, Any], width: int = 100, *, color: bool = False,
                 selected: int = 0, detail: bool | str = False, live: bool = False,
                 frame: bool = False, animate: bool = True, now: float | None = None) -> list[str]:
    """Render a compact, clipped dashboard frame with optional trusted SGR."""

    width = max(1, int(width or 1))
    now = time.time() if now is None else now
    indicator = "●" if (frame or not animate) else "○"
    title = f"codex-monitor dashboard  {_paint('LIVE VIEW ' + indicator if live else 'SNAPSHOT', 36, color)}  {_paint('[read-only]', 2, color)}"
    lines = [title]
    receiver = snapshot.get("receiver") or {}
    receiver_state = _receiver_status(receiver)
    process = (
        "alive" if receiver.get("process_alive") is True else
        "stopped" if receiver.get("process_alive") is False else
        "unknown"
    )
    readiness = "ready" if receiver.get("ready") else "not ready"
    reason = receiver.get("reason")
    lines.append(
        f"Receiver {_status(receiver_state, color)} · process {process} · /v1/status {readiness}"
        + (f" [{_safe(receiver.get('health'))}]" if receiver.get("health") else "")
        + (f" ({_safe(reason)})" if reason else "")
    )
    lines.append(_refresh_line(snapshot, now))
    lines.append(
        "Status: " + " ".join(
            f"{_status(label, color)} {description}" for label, description in (
                ("ON", "enabled/ready"), ("OFF", "paused/stopped"),
                ("STALE", "old/unhealthy"), ("UNKNOWN", "unavailable"),
            )
        )
    )
    if not snapshot.get("ok"):
        lines.append("ERROR: " + _safe(snapshot.get("error"), max(1, width - 7)))
        lines.append("The state inventory will be retried while the dashboard is running.")
        return [_clip(line, width) for line in lines]
    if snapshot.get("thread_filter"):
        lines.append("Filter: thread=" + _safe(snapshot["thread_filter"]))
    lines.append("CONVERSATIONS")
    lines.append("    " + _paint("STATE   BINDING              ENDPOINT        EVENTS                    COLLECTOR", 1, color))
    rows = _binding_rows(snapshot)
    if not rows:
        lines.append("  No conversation bindings found.")
    for connection in snapshot.get("connections") or []:
        lines.append("  Conversation: " + _safe(connection.get("thread")))
        bindings = connection.get("bindings") or []
        for binding in bindings:
            row_index = next(index for conn, item, index in rows if conn is connection and item is binding)
            collectors = [item for item in connection.get("collectors", []) if item.get("binding") == binding.get("name")]
            collector_names = ",".join(_safe(item.get("name"), 16) for item in collectors) or "-"
            line = (
                f"  {'▶' if row_index == selected else ' '} "
                f"{_status_cell(_binding_status(binding), color)}"
                f"{_fit(binding.get('name'), 20)} "
                f"{_fit(binding.get('endpoint'), 14)} "
                f"{_fit(_event_compact(binding.get('events', {})), 25)} "
                f"{_fit(collector_names, 18)}"
            )
            if row_index == selected and color:
                line = "\x1b[7m" + line + "\x1b[0m"
            lines.append(line)
            if detail == "all" or (detail and row_index == selected):
                lines.append("")
                lines.extend(_detail_lines(connection, binding, color))
    if snapshot.get("warnings"):
        lines.append("Warnings: " + "; ".join(_safe(value) for value in snapshot["warnings"]))
    return [_clip(line, width) for line in lines]


def render_text(snapshot: dict[str, Any], *, width: int = 100, height: int = 24, scroll: int = 0,
                color: bool = False, selected: int = 0, detail: bool | str = False, live: bool = False,
                frame: bool = False, animate: bool = True, now: float | None = None) -> str:
    """Render a bounded viewport with a persistent header and compact table."""

    width = max(1, int(width or 1))
    height = max(2, int(height or 2))
    body = render_lines(snapshot, width, color=color, selected=selected, detail=detail,
                        live=live, frame=frame, animate=animate, now=now)
    # Title, receiver state and refresh age stay pinned.  The compact legend
    # scrolls with the table so it does not consume most of a short terminal.
    header_count = min(3, len(body))
    header = body[:header_count]
    table = body[header_count:]
    available = max(1, height - 1)
    body_slots = max(0, available - header_count)
    maximum = max(0, len(table) - body_slots)
    offset = min(max(int(scroll), 0), maximum)
    # Selection is global across grouped conversation headers.  Keep the
    # highlighted row visible even when the table has many groups.
    selected_line = next(
        (index for index, line in enumerate(table) if _ANSI_SGR.sub("", line).startswith("  ▶ ")),
        None,
    )
    if selected_line is not None and body_slots:
        if detail is True:
            # Details are inserted immediately after the selected row. Start
            # that panel at the selected row so ``d`` is useful even for the
            # last binding in a long, multi-conversation inventory.
            offset = min(maximum, max(0, selected_line - 1))
        elif selected_line < offset:
            offset = selected_line
        elif selected_line >= offset + body_slots:
            offset = min(maximum, selected_line - body_slots + 1)
    visible = header + table[offset:offset + body_slots]
    visible = visible[:available]
    visible += [""] * (available - len(visible))
    footer = (
        f"q/Ctrl-C quit · j/k scroll/select · d details · "
        f"{offset + 1}-{min(offset + len(table), len(table))}/{len(table)}"
    )
    return "\n".join(visible + [_clip(footer, width)])


def _key(stdin: TextIO) -> str | None:
    fd = stdin.fileno()
    ready, _, _ = select.select([fd], [], [], 0)
    if not ready:
        return None
    value = os.read(fd, 1)
    if value != b"\x1b":
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return None
    sequence = bytearray(value)
    # Read only the short escape sequences used for navigation.  Reading
    # from the file descriptor avoids TextIO buffering arrow bytes.
    while len(sequence) < 6:
        ready, _, _ = select.select([fd], [], [], .015)
        if not ready:
            break
        sequence.extend(os.read(fd, 1))
        if sequence[-1:] in (b"A", b"B", b"H", b"F"):
            break
        if sequence[-1:] == b"~":
            break
    return {
        b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[5~": "pageup",
        b"\x1b[6~": "pagedown", b"\x1b[H": "home", b"\x1b[F": "end",
    }.get(bytes(sequence))


def _color_enabled(mode: str, stream: TextIO, environ: dict[str, str] | None = None) -> bool:
    if mode not in {"auto", "always", "never"}:
        raise ValueError("dashboard color must be auto, always or never")
    if mode == "never":
        return False
    environ = os.environ if environ is None else environ
    if mode == "auto":
        return bool(getattr(stream, "isatty", lambda: False)()) and environ.get("TERM") != "dumb" and not environ.get("NO_COLOR")
    return True


def _draw_frame(stdout: TextIO, rendered: str) -> None:
    """Rewrite rows in place without clearing the whole alternate screen."""

    rows = rendered.splitlines()
    stdout.write("\x1b[H")
    for index, row in enumerate(rows):
        stdout.write("\x1b[2K" + row)
        if index + 1 < len(rows):
            stdout.write("\n")
    # Clear only stale rows below the new viewport.  In particular, do not
    # emit CSI 2J on every animation frame; that causes visible flicker.
    stdout.write("\x1b[J")


def run_dashboard(root: str | os.PathLike[str], *, once: bool = False, as_json: bool = False,
                  interval: float = 2.0, thread: str | None = None,
                  color: str = "auto", animate: bool = True,
                  stdout: TextIO | None = None, stdin: TextIO | None = None) -> int:
    """Run one dashboard render or the interactive TTY view."""

    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("dashboard interval must be a positive finite number")
    if as_json and not once:
        raise ValueError("--json is only available with --once")
    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    color_enabled = _color_enabled(color, stdout)
    # Snapshot output is commonly redirected or captured.  Keep it stable
    # and copy/paste friendly in auto mode even if a caller supplies a TTY;
    # --color always remains an explicit opt-in.
    if once and color == "auto":
        color_enabled = False
    reader = DashboardReader(root, thread=thread)
    if once:
        snapshot = reader.snapshot()
        if as_json:
            json.dump(snapshot, stdout, ensure_ascii=False, indent=2, sort_keys=True)
            stdout.write("\n")
        else:
            width = shutil.get_terminal_size((100, 24)).columns if stdout.isatty() else 100
            stdout.write("\n".join(render_lines(snapshot, width, color=color_enabled, detail="all", animate=animate)))
            stdout.write("\n")
        stdout.flush()
        return 0 if snapshot.get("ok") else 2
    if not stdout.isatty() or not stdin.isatty():
        raise ValueError("live dashboard requires a TTY; use dashboard --once for non-interactive output")
    import termios
    import tty
    old = termios.tcgetattr(stdin.fileno())
    scroll = 0
    selected = 0
    detail = False
    try:
        tty.setcbreak(stdin.fileno())
        stdout.write("\x1b[?1049h\x1b[?25l")
        # The first snapshot is synchronous so a healthy state appears
        # immediately.  Later reads run in a daemon worker; a slow status
        # probe therefore cannot freeze the 0.5 second live animation.
        snapshot = reader.snapshot()
        poll_lock = threading.Lock()
        poll_running = False
        pending_snapshot: dict[str, Any] | None = None

        def start_poll() -> None:
            nonlocal poll_running
            with poll_lock:
                if poll_running:
                    return
                poll_running = True

            def poll() -> None:
                nonlocal poll_running, pending_snapshot
                try:
                    value = reader.snapshot()
                    with poll_lock:
                        pending_snapshot = value
                finally:
                    with poll_lock:
                        poll_running = False

            threading.Thread(target=poll, name="codex-monitor-dashboard-read", daemon=True).start()

        next_poll = time.monotonic() + float(interval)
        next_frame = time.monotonic()
        frame = False
        while True:
            now_mono = time.monotonic()
            with poll_lock:
                if pending_snapshot is not None:
                    snapshot = pending_snapshot
                    pending_snapshot = None
            if now_mono >= next_poll:
                start_poll()
                next_poll += float(interval)
                if next_poll <= now_mono:
                    next_poll = now_mono + float(interval)
            if now_mono >= next_frame:
                frame = not frame
                next_frame = now_mono + (0.5 if animate else float(interval))
                size = shutil.get_terminal_size((100, 24))
                rows = _binding_rows(snapshot)
                selected = min(max(selected, 0), max(0, len(rows) - 1))
                rendered = render_text(
                    snapshot, width=size.columns, height=size.lines, scroll=scroll,
                    color=color_enabled, selected=selected, detail=detail, live=True,
                    frame=frame, animate=animate,
                )
                _draw_frame(stdout, rendered)
                stdout.flush()
            wait = min(.1, max(0.0, next_frame - now_mono), max(0.0, next_poll - now_mono))
            ready, _, _ = select.select([stdin], [], [], wait)
            if not ready:
                continue
            value = _key(stdin)
            if value in ("q", "Q", "\x03"):
                return 0
            rows = _binding_rows(snapshot)
            if value in ("j", "down"):
                selected = min(selected + 1, max(0, len(rows) - 1))
            elif value in ("k", "up"):
                selected = max(0, selected - 1)
            elif value == "pageup":
                selected = max(0, selected - max(1, size.lines - 2))
            elif value == "pagedown":
                selected = min(max(0, len(rows) - 1), selected + max(1, size.lines - 2))
            elif value == "home":
                selected = 0
            elif value == "end":
                selected = max(0, len(rows) - 1)
            elif value == "d":
                detail = not detail
            else:
                continue
            # Keep the selected row visible while preserving the old scroll
            # behavior for callers that use render_text directly.
            scroll = max(0, selected - max(1, size.lines - 7))
            next_frame = 0.0
    except KeyboardInterrupt:
        return 0
    finally:
        # Discard dashboard navigation left in the input queue before giving
        # control back to the shell. On macOS TCSADRAIN also leaves PENDIN set
        # when switching from cbreak to canonical input.
        termios.tcsetattr(stdin.fileno(), termios.TCSAFLUSH, old)
        stdout.write("\x1b[?25h\x1b[0m\x1b[?1049l")
        stdout.flush()
