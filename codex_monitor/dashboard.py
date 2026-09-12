"""Read-only dashboard snapshots with explicit, scoped monitor controls.

Refreshes use SQLite mode=ro and never construct a runtime. Only an explicit
user action invokes Monitor; opening a conversation launches the native TUI.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import re
import stat as stat_module
import unicodedata
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .owner_health import OwnerHealthCache, probe_owner
from .session import server_token


READ_TIMEOUT = 0.35
STATUS_TIMEOUT = 0.75
OWNER_HEALTH_TIMEOUT = 0.45
OWNER_HEALTH_BUDGET = 3.0
STATUS_BODY_LIMIT = 64 * 1024
CHECKPOINT_LIMIT = 64 * 1024
MAX_TEXT = 240


_DASHBOARD_SCOPE = {
    "event_delivery": "persisted_observations",
    "request_lifecycle": "explicit_work_reports",
    "model_telemetry": "not_collected",
    "tool_telemetry": "not_collected",
}


def _dashboard_scope() -> dict[str, str]:
    """Return the dashboard's machine-readable reporting boundary."""

    return dict(_DASHBOARD_SCOPE)


class DashboardError(RuntimeError):
    """A state inventory could not be read safely."""


def _open_endpoint(endpoint: Any) -> str:
    """Validate one stored owner endpoint for an explicit TUI resume."""

    if (
        not isinstance(endpoint, str) or not endpoint or
        any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in endpoint)
    ):
        raise DashboardError("selected binding has an invalid owner endpoint")
    if endpoint in {"shared-local", "local"}:
        raise DashboardError("shared-local has no owner address; configure an explicit owner endpoint")
    if endpoint.startswith("unix://"):
        path = endpoint.removeprefix("unix://")
        if not path.startswith("/") or not path[1:] or "\x00" in path:
            raise DashboardError("selected binding uses a non-absolute Unix owner endpoint")
        return endpoint
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed.port  # Force malformed and out-of-range ports to fail here.
    except ValueError as exc:
        raise DashboardError("selected binding has a malformed owner endpoint") from exc
    if (
        parsed.scheme not in {"ws", "wss"} or not parsed.netloc or not hostname or
        any(char.isspace() for char in endpoint) or parsed.netloc.endswith(":")
    ):
        raise DashboardError("selected binding endpoint is not a supported ws/wss or absolute Unix owner")
    if parsed.username or parsed.password or parsed.fragment:
        raise DashboardError("selected binding endpoint cannot contain credentials or a fragment")
    if parsed.scheme == "ws" and hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise DashboardError("insecure ws owner endpoints must use loopback")
    return endpoint


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
        return f"{int(age)}s"
    if age < 3600:
        return f"{int(age) // 60}m {int(age) % 60}s"
    if age < 86400:
        return f"{int(age) // 3600}h {int(age) % 3600 // 60}m"
    return f"{int(age) // 86400}d {int(age) % 86400 // 3600}h"


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
        "project": "Ungrouped",
        "display_name": "",
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


def _unverified_owner(endpoint: Any, *, reason: str) -> dict[str, Any]:
    """Describe a route whose owner cannot be probed without opening stdio."""

    return {
        "endpoint": _safe(endpoint),
        "status": "unverified",
        "state": "unverified",
        "ready": False,
        "transport_reachable": None,
        "thread_loaded": None,
        "account": {
            "status": "unverified",
            "present": None,
            "credential_validation": "unverified",
        },
        "credential_validation": "unverified",
        "model_execution": "unverified",
        "reason": reason,
    }


def _dashboard_owner_probe(endpoint: str, thread: str) -> dict[str, Any]:
    """Probe with the same optional token source used by the explicit opener."""

    try:
        token = server_token()
    except Exception:
        # Do not expose invalid token-file paths or token contents in a
        # dashboard snapshot. The owner probe remains an authentication fact.
        return {
            "endpoint": _safe(endpoint),
            "thread": _safe(thread),
            "status": "auth-required",
            "state": "auth-required",
            "ready": False,
            "transport_reachable": None,
            "thread_loaded": None,
            "account": {"status": "unverified", "present": None,
                        "credential_validation": "unverified"},
            "credential_validation": "unverified",
            "model_execution": "unverified",
            "reason": "authentication required",
        }
    return probe_owner(endpoint, thread, token=token, timeout=OWNER_HEALTH_TIMEOUT)


class DashboardReader:
    """Build one bounded, read-only state inventory.

    Owner health is deliberately injectable so inventory tests can remain
    local and deterministic.  ``owner_health=False`` disables the explicit
    owner probe; it never changes the persisted delivery inventory.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        thread: str | None = None,
        clock=time.time,
        owner_health: bool = True,
        owner_probe=None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.thread = thread
        self.clock = clock
        self._owner_health = bool(owner_health)
        if owner_probe is False:
            self._owner_health = False
            owner_probe = None
        self._owner_cache = (
            OwnerHealthCache(probe=owner_probe or _dashboard_owner_probe)
            if self._owner_health else None
        )

    def _attach_owner_health(self, connections: list[dict[str, Any]]) -> None:
        """Attach cached owner observations without creating local runtimes."""

        deadline = time.monotonic() + OWNER_HEALTH_BUDGET
        endpoint_observations: dict[str, dict[str, Any]] = {}
        for connection in connections:
            thread = connection.get("thread")
            for binding in connection.get("bindings") or []:
                endpoint = binding.get("endpoint")
                if not self._owner_health or self._owner_cache is None:
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="owner probe disabled",
                    )
                    continue
                if not isinstance(thread, str) or not thread:
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="conversation identity is unavailable",
                    )
                    continue
                if not isinstance(endpoint, str) or not binding.get("identity_exact"):
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="route identity is not exact",
                    )
                    continue
                if endpoint in {"shared-local", "local"}:
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="shared-local/local owner is not explicitly probeable",
                    )
                    continue
                if not endpoint.startswith(("ws://", "wss://", "unix://")):
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="owner endpoint is not a supported WebSocket transport",
                    )
                    continue
                if time.monotonic() >= deadline:
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="owner probe budget exhausted",
                    )
                    continue
                try:
                    observed = endpoint_observations.get(endpoint)
                    if observed is None:
                        observed = self._owner_cache.get(endpoint, thread)
                        endpoint_observations[endpoint] = observed
                    elif isinstance(observed.get("loaded_threads"), list):
                        # One endpoint probe already fetched the bounded loaded
                        # list. Reuse that observation for sibling routes and
                        # avoid reconnecting once per thread every refresh.
                        observed = copy.deepcopy(observed)
                        loaded = set(observed.get("loaded_threads") or ())
                        observed["thread"] = thread
                        observed["thread_loaded"] = thread in loaded
                        if observed["status"] in {"ready-to-receive", "unloaded"}:
                            if thread in loaded:
                                observed.update(
                                    status="ready-to-receive",
                                    state="ready-to-receive",
                                    ready=True,
                                    reason="owner transport reachable and conversation loaded",
                                )
                            else:
                                observed.update(
                                    status="unloaded",
                                    state="unloaded",
                                    ready=False,
                                    reason="conversation is not loaded by this owner",
                                )
                        observed.pop("thread_read", None)
                    else:
                        observed = copy.deepcopy(observed)
                        observed["thread"] = thread
                    binding["owner_health"] = observed
                except Exception:
                    # An injected probe is test/application code; retain the
                    # dashboard's bounded read-only contract if it fails.
                    binding["owner_health"] = _unverified_owner(
                        endpoint,
                        reason="owner probe unavailable",
                    )

    def selected_binding(self, name: str, thread: str, displayed_endpoint: str) -> dict[str, str]:
        """Re-read one exact binding from the read-only database before launch."""

        if (
            not isinstance(name, str) or not isinstance(thread, str) or
            not isinstance(displayed_endpoint, str) or not name or not thread
        ):
            raise DashboardError("selected conversation identity is invalid")
        if (
            any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in name + thread) or
            thread.startswith("-")
        ):
            raise DashboardError("selected conversation identity is unsafe")
        db = _ro_connect(self.root / "monitor.sqlite3")
        try:
            tables = _tables(db)
            if "bindings" not in tables:
                raise DashboardError("monitor database is missing the bindings table")
            row = db.execute(
                "SELECT name,thread,endpoint FROM bindings WHERE name=? AND thread=?"
                + (" AND removed=0" if "removed" in _columns(db, "bindings") else ""),
                (name, thread),
            ).fetchone()
            if row is None:
                raise DashboardError("selected binding no longer exists")
            if (
                row["name"] != name or
                row["thread"] != thread or
                row["endpoint"] != displayed_endpoint
            ):
                raise DashboardError("selected binding identity changed; refresh the dashboard")
            endpoint = _open_endpoint(row["endpoint"])
            return {"name": row["name"], "thread": row["thread"], "endpoint": endpoint}
        except sqlite3.Error as exc:
            raise DashboardError(f"cannot re-read selected binding ({type(exc).__name__})") from exc
        finally:
            db.close()

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
            ) + " FROM bindings" + (" WHERE removed=0" if "removed" in binding_columns else "") + " ORDER BY thread,name"
            for row in db.execute(binding_query):
                source_list, source_error = _parse_sources(row["sources"])
                item = {
                    "name": _safe(row["name"]),
                    "thread": _safe(row["thread"]),
                    "endpoint": _safe(row["endpoint"]),
                    # Reject display-safe/truncated identities before they
                    # can be resolved to a different stored binding.
                    "identity_exact": all(
                        _safe(row[field]) == row[field]
                        for field in ("name", "thread", "endpoint")
                    ),
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
            if "conversation_metadata" in tables:
                for row in db.execute("SELECT thread,project,display_name FROM conversation_metadata"):
                    if row["thread"] in connections:
                        connections[row["thread"]]["project"] = _safe(row["project"]) or "Ungrouped"
                        connections[row["thread"]]["display_name"] = _safe(row["display_name"])
            result = sorted(connections.values(), key=lambda value: (
                value["project"].casefold(), _conversation_label(value).casefold(), value["thread"]))
            labels: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for value in result:
                labels.setdefault((value["project"].casefold(), _conversation_label(value).casefold()), []).append(value)
            for duplicates in labels.values():
                if len(duplicates) > 1:
                    suffixes = [item["thread"][-8:] for item in duplicates]
                    for item in duplicates:
                        item["display_suffix"] = item["thread"][-8:] if len(set(suffixes)) == len(suffixes) else item["thread"]
            for value in result:
                value["requests"] = _request_inventory(self.root, value["thread"], now, deadline)
            inventory = result
        except sqlite3.Error as exc:
            raise DashboardError(f"monitor database is corrupt or unreadable ({type(exc).__name__})") from exc
        finally:
            db.close()
        # Network probes run after the SQLite snapshot is closed. This keeps a
        # slow or unavailable owner from holding a read transaction open.
        self._attach_owner_health(inventory)
        return inventory, []

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
                "scope": _dashboard_scope(),
                "error": _safe(str(exc)),
                "receiver": self._receiver(),
                "connections": [],
            }
        return {
            "ok": True,
            "read_only": True,
            "generated_at": now,
            "scope": _dashboard_scope(),
            "thread_filter": _safe(self.thread) if self.thread is not None else None,
            "receiver": self._receiver(),
            "connections": connections,
            "warnings": warnings,
            "note": "Dashboard scope: persisted event-delivery observations, explicit request lifecycle work reports, and bounded read-only explicit-owner readiness probes; live model, tool, and external source telemetry are not collected.",
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


_STATUS_DOTS = {
    "ERROR": ("●", 31),
    "ON": ("●", 32),
    "OFF": ("●", 31),
    "STALE": ("●", 33),
    "UNKNOWN": ("●", 90),
}
_STATUS_PLAIN_DOTS = {"ERROR": "!", "ON": "●", "OFF": "○", "STALE": "◐", "UNKNOWN": "·"}
_STATUS_WORDS = {"ERROR": "error", "ON": "enabled", "OFF": "paused", "STALE": "stale", "UNKNOWN": "unavailable"}


def _status_dot(value: str, color: bool) -> str:
    """Render a compact status dot, with shape carrying no-color meaning."""

    dot, code = _STATUS_DOTS.get(value, _STATUS_DOTS["UNKNOWN"])
    return _paint(dot if color else _STATUS_PLAIN_DOTS.get(value, "·"), code, color)


def _status_word(value: str) -> str:
    return _STATUS_WORDS.get(value, "unavailable")


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


def _owner_health_status(binding: dict[str, Any]) -> str:
    """Map an owner observation to the existing compact row vocabulary."""

    health = binding.get("owner_health") or {}
    status = health.get("status") or health.get("state")
    if status == "auth-required":
        return "ERROR"
    if status in {"unavailable", "unloaded"}:
        return "STALE"
    if status == "ready-to-receive":
        return "ON"
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


def _event_summary(value: dict[str, Any]) -> str:
    counts = value.get("counts") or {}
    rendered = ", ".join(f"{count} {_event_state(key, technical=True)}" for key, count in sorted(counts.items())) or "No delivery events"
    latest = value.get("latest")
    if latest:
        identifier = latest.get("delivery_id") or latest.get("request_id")
        rendered += f"; latest delivery {_event_state(latest.get('state'), technical=True)} {_safe(identifier)} ({_age_text(latest.get('age_seconds'))})"
    return rendered


def _work_report_summary(value: dict[str, Any]) -> str:
    """Summarize explicit request lifecycle reports without calling them events."""

    counts = value.get("counts") or {}
    rendered = ", ".join(f"{count} {_event_state(key, technical=True)}" for key, count in sorted(counts.items())) or "No work reports"
    latest = value.get("latest")
    if latest:
        identifier = latest.get("request_id")
        rendered += f"; latest report {_event_state(latest.get('state'), technical=True)} {_safe(identifier)} ({_age_text(latest.get('age_seconds'))})"
    return rendered


def _event_state(value: Any, *, technical: bool = False) -> str:
    known = {
        "pending": "pending",
        "accepted": "sent to Codex",
        "in_progress": "in progress",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "expired": "expired",
        "received": "received",
        "acknowledged": "acknowledged",
    }
    return known.get(str(value), _safe(value).replace("_", " ") if technical else "delivery updated")


def _event_compact(value: dict[str, Any]) -> str:
    """Summarize delivery observations without technical identifiers."""

    counts = value.get("counts") or {}
    rendered = ", ".join(f"{count} {_event_state(key)}" for key, count in sorted(counts.items()))
    latest = value.get("latest")
    if latest:
        latest_state = _event_state(latest.get("state"))
        counted_states = {_event_state(key) for key in counts}
        recent = (
            f"delivery updated {_age_text(latest.get('age_seconds'))} ago"
            if latest_state in counted_states else
            f"{latest_state} {_age_text(latest.get('age_seconds'))} ago"
        )
        rendered = f"{rendered} · {recent}" if rendered else recent
    return rendered or "—"


def _binding_rows(snapshot: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    rows: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    index = 0
    for connection in snapshot.get("connections") or []:
        for binding in connection.get("bindings") or []:
            rows.append((connection, binding, index))
            index += 1
    return rows


def _preferred_route(connection: dict[str, Any]) -> int:
    """Choose an explicit enabled owner route before shared/local fallbacks."""

    bindings = connection.get("bindings") or []
    ranked: list[tuple[int, int]] = []
    for index, binding in enumerate(bindings):
        endpoint = binding.get("endpoint")
        explicit = isinstance(endpoint, str) and endpoint.startswith(("ws://", "wss://", "unix://"))
        rank = (
            0 if binding.get("enabled") is True and explicit else
            1 if binding.get("enabled") is True else
            2 if explicit else 3
        )
        ranked.append((rank, index))
    return min(ranked, default=(0, 0))[1]


def _conversation_status(connection: dict[str, Any]) -> str:
    """Report known faults without treating enabled delivery as model readiness."""

    bindings = connection.get("bindings") or []
    for binding in bindings:
        events = binding.get("events") or {}
        latest = events.get("latest") or {}
        counts = events.get("counts") or {}
        if counts.get("dead", 0) or latest.get("error") or binding.get("schema_error"):
            return "ERROR"
    if any((binding.get("events") or {}).get("counts", {}).get("uncertain", 0)
           for binding in bindings):
        return "STALE"
    collector_states = [_collector_status(item) for item in connection.get("collectors") or []]
    if "STALE" in collector_states:
        return "STALE"
    owner_states = [_owner_health_status(binding) for binding in bindings]
    if "ERROR" in owner_states:
        return "ERROR"
    statuses = [_binding_status(binding) for binding in bindings]
    if statuses and all(value == "OFF" for value in statuses):
        return "OFF"
    if "ON" in owner_states:
        return "ON"
    if "STALE" in owner_states:
        return "STALE"
    # Queue acceptance and a live collector say nothing about authentication or
    # successful model execution. Explicit owner observations are attached
    # separately and only establish readiness to receive.
    return "UNKNOWN"


def _conversation_label(connection: dict[str, Any]) -> str:
    """Derive a stable human route label without rewriting identifiers."""

    if connection.get("display_name"):
        label = _safe(connection["display_name"])
    else:
        label = _derived_conversation_label(connection)
    suffix = connection.get("display_suffix")
    return f"{_clip(label, 24)} · {suffix}" if suffix else label


def _derived_conversation_label(connection: dict[str, Any]) -> str:
    bindings = connection.get("bindings") or []
    entries = [(_binding_label(connection, binding), binding) for binding in bindings]
    nonmanaged = [label for label, binding in entries if "managed/file" not in (binding.get("sources") or [])]
    labels = nonmanaged or [label for label, _ in entries]
    labels = list(dict.fromkeys(label for label in labels if label))
    if not labels:
        return "Unnamed conversation"
    source_ids = list(dict.fromkeys(
        source for _, binding in entries
        for source in (binding.get("sources") or [])
        if source != "managed/file" and isinstance(source, str) and source
    ))
    for source in source_ids:
        prefix = source.casefold() + "-"
        remainders = [label[len(source) + 1:] for label in labels if label.casefold().startswith(prefix)]
        if len(remainders) == len(labels) and all(remainders):
            labels = remainders
            break
    if len(labels) == 1:
        return labels[0]
    generic = {"managed", "watch", "route", "binding", "connection"}
    tokenized = [re.split(r"[\s:_/.-]+", label.strip()) for label in labels]
    common: list[str] = []
    for parts in zip(*tokenized):
        if len({part.casefold() for part in parts}) != 1:
            break
        common.append(parts[0])
    if common:
        candidate = "-".join(common)
        if len(candidate) >= 3 and candidate.casefold() not in generic:
            return candidate
    return f"{labels[0]} + {len(labels) - 1} routes"


def _conversation_activity(connection: dict[str, Any]) -> str:
    """Aggregate recent event-delivery observations into one human phrase."""

    owner_states = [
        (binding.get("owner_health") or {}).get("status")
        for binding in connection.get("bindings") or []
    ]
    if "auth-required" in owner_states:
        return "Owner authentication required"
    if "unavailable" in owner_states:
        return "Owner unavailable"
    if "unloaded" in owner_states:
        return "Conversation unloaded from owner"

    counts: dict[str, int] = {}
    latest: dict[str, Any] | None = None
    for binding in connection.get("bindings") or []:
        events = binding.get("events") or {}
        for state, count in (events.get("counts") or {}).items():
            try:
                counts[state] = counts.get(state, 0) + int(count)
            except (TypeError, ValueError):
                continue
        candidate = events.get("latest")
        candidate_age = _number(candidate.get("age_seconds")) if candidate else None
        latest_age = _number(latest.get("age_seconds")) if latest else None
        if candidate and (
            latest is None or
            candidate_age is not None and (latest_age is None or candidate_age < latest_age)
        ):
            latest = candidate
    if counts.get("dead", 0):
        return f"{counts['dead']} delivery failed"
    if latest and latest.get("error"):
        return "Delivery error · see details"
    if latest is None and not counts:
        if "ready-to-receive" in owner_states:
            return "Ready to receive · model execution unverified"
        return "Codex readiness unverified"
    if latest is not None:
        state = latest.get("state")
        count = counts.get(state, 1)
        return f"{count} {_event_state(state)} · {_age_text(latest.get('age_seconds'))} ago"
    state, count = max(counts.items(), key=lambda item: item[1])
    return f"{count} {_event_state(state)}"


def _conversation_summary(connection: dict[str, Any]) -> str:
    """Describe the selected conversation's routes without exposing endpoints."""

    bindings = connection.get("bindings") or []
    source_names: list[str] = []
    file_monitors = 0
    for binding in bindings:
        sources = binding.get("sources") or []
        if "managed/file" in sources:
            file_monitors += 1
        for source in sources:
            if source != "managed/file" and source not in source_names:
                source_names.append(_safe(source).replace("_", " "))
    parts = [name.upper() if len(name) <= 3 else name.title() for name in source_names]
    if file_monitors:
        parts.append(f"{file_monitors} file monitor" + ("s" if file_monitors != 1 else ""))
    if not parts:
        return f"{len(bindings)} connection" + ("s" if len(bindings) != 1 else "")
    return " + ".join(parts)


def _selected_binding_index(snapshot: dict[str, Any], selected: int, selected_route: int) -> int:
    connections = snapshot.get("connections") or []
    if selected < 0 or selected >= len(connections):
        raise DashboardError("selected conversation is out of range")
    bindings = connections[selected].get("bindings") or []
    if not bindings:
        raise DashboardError("selected conversation has no routes")
    route = selected_route % len(bindings)
    target = bindings[route]
    rows = _binding_rows(snapshot)
    for index, (connection, binding, _) in enumerate(rows):
        if connection is connections[selected] and binding is target:
            return index
    raise DashboardError("selected route is no longer visible; refresh the dashboard")


def _cycle_route(connection: dict[str, Any], selected_route: int) -> int:
    """Advance the selected conversation route for Tab navigation."""

    total = len(connection.get("bindings") or [])
    return (selected_route + 1) % total if total else 0


def _refresh_line(snapshot: dict[str, Any], now: float) -> str:
    timestamp = _number(snapshot.get("generated_at"))
    if timestamp is None:
        return "Refresh time unavailable"
    age = max(0.0, now - timestamp)
    return f"Updated {_age_text(age)} ago"


def _binding_label(connection: dict[str, Any], binding: dict[str, Any]) -> str:
    """Use the human managed-watch name when a generated binding has one."""

    name = binding.get("name")
    for collector in connection.get("collectors") or []:
        if collector.get("binding") == name and collector.get("name"):
            return _safe(collector.get("name"))
    if isinstance(name, str) and name.startswith("managed-"):
        return "managed watch"
    return _safe(name)


def _fit(text: Any, width: int) -> str:
    """Fit a trusted display cell while preserving any approved SGR."""

    raw = str(text)
    value = _clip(raw if _ANSI_SGR.search(raw) else _safe(raw), max(0, width))
    visible = sum(_cell_width(char) for char in _ANSI_SGR.sub("", value))
    return value + " " * max(0, width - visible)


def _detail_lines(connection: dict[str, Any], binding: dict[str, Any], color: bool) -> list[str]:
    """Render details only after the user explicitly asks for them."""

    label = _binding_label(connection, binding)
    raw_name = _safe(binding.get("name"))
    lines = [f"  Details: {label} in {_safe(connection.get('thread'))}"]
    if raw_name != label:
        lines.append("    binding id: " + raw_name)
    lines.append(
        f"    status {_status_dot(_binding_status(binding), color)} {_status_word(_binding_status(binding))} · endpoint {_safe(binding.get('endpoint'))} · "
        f"sources {', '.join(_safe(source) for source in binding.get('sources', [])) or 'none'}"
    )
    if binding.get("schema_error"):
        lines.append("    schema error: " + _safe(binding["schema_error"]))
    lines.append("    delivery events: " + _event_summary(binding.get("events", {})))
    latest = (binding.get("events") or {}).get("latest") or {}
    if latest.get("error"):
        lines.append("    delivery error: " + _safe(latest["error"]))
    legacy_owner_line = "owner_health" not in binding
    if not legacy_owner_line:
        owner = binding["owner_health"]
        owner_status = _safe(owner.get("status") or owner.get("state"))
        owner_reason = owner.get("reason")
        owner_line = f"    Codex owner: {owner_status}"
        if owner.get("transport_reachable") is True:
            owner_line += " · transport reachable"
        if owner.get("thread_loaded") is True:
            owner_line += " · conversation loaded"
        if owner_reason:
            owner_line += " · " + _safe(owner_reason)
        lines.append(owner_line)
        account = owner.get("account") or {}
        if account.get("status") in {"present", "absent"}:
            lines.append(
                "    account observation: " + _safe(account.get("status")) +
                " · credential validation unverified"
            )
        thread_read = owner.get("thread_read") or {}
        if thread_read.get("status"):
            lines.append("    thread status observed: " + _safe(thread_read.get("status")))
    requests = connection.get("requests") or {}
    lines.append("    work reports (request lifecycle): " + (_work_report_summary(requests) if requests.get("available") else _safe(requests.get("reason", "unavailable"))))
    if legacy_owner_line:
        # Keep hand-built snapshots and older callers honest without changing
        # their compact detail layout.
        lines.append("    Codex authentication and model readiness: unverified")
    else:
        lines.append("    model execution: unverified (dashboard does not poll turns)")
    collectors = [item for item in connection.get("collectors", []) if item.get("binding") == binding.get("name")]
    for collector in collectors:
        observation = collector.get("checkpoint", {}).get("last_observation") or {}
        observed = _safe(observation.get("state"), 40) if observation else "none"
        lines.append(
            f"    collector {_safe(collector.get('name'))}: {_status_dot(_collector_status(collector), color)} "
            f"{_status_word(_collector_status(collector))}; "
            f"seen {_age_text(collector.get('worker_seen_age_seconds'))}/{_safe(collector.get('worker_seen_status'))}; sample={observed}"
        )
        errors = [collector.get("last_error"), collector.get("last_sample_error"), collector.get("checkpoint", {}).get("error")]
        errors = [_safe(error) for error in errors if error]
        if errors:
            lines.append("      error: " + "; ".join(errors))
    return lines


def render_lines(snapshot: dict[str, Any], width: int = 100, *, color: bool = False,
                 selected: int = 0, selected_route: int = 0, detail: bool | str = False, live: bool = False,
                 frame: bool = False, animate: bool = True, now: float | None = None) -> list[str]:
    """Render the calm overview and optional technical details."""

    width = max(1, int(width or 1))
    now = time.time() if now is None else now
    pulse = "●" if (frame or not animate) else "○"
    title_right = f"{_paint(pulse, 36, color)} Live" if live else "Snapshot"
    title_left = _paint("codex-monitor", 1, color)
    title_gap = max(2, width - len(_ANSI_SGR.sub("", title_left)) - len(_ANSI_SGR.sub("", title_right)))
    lines = [title_left + " " * title_gap + title_right]
    lines.append(_paint("─" * width, 90, color))
    receiver = snapshot.get("receiver") or {}
    receiver_state = _receiver_status(receiver)
    if detail:
        process = (
            "alive" if receiver.get("process_alive") is True else
            "stopped" if receiver.get("process_alive") is False else
            "unknown"
        )
        readiness = "ready" if receiver.get("ready") else "not ready"
        receiver_text = f"{_status_dot(receiver_state, color)} Receiver · process {process} · status probe {readiness}"
        if receiver.get("health"):
            receiver_text += " · health " + _safe(receiver.get("health"))
        if receiver.get("reason"):
            receiver_text += " · " + _safe(receiver.get("reason"))
    else:
        receiver_text = f"{_status_dot(receiver_state, color)} Receiver"
    if not snapshot.get("ok"):
        lines.append(receiver_text)
        lines.append("ERROR: " + _safe(snapshot.get("error"), max(1, width - 7)))
        lines.append("The state inventory will be retried while the dashboard is running.")
        return [_clip(line, width) for line in lines]
    if snapshot.get("thread_filter"):
        lines.append("Filtered conversation")
    connections = snapshot.get("connections") or []
    rows = _binding_rows(snapshot)
    connection_word = "connection" if len(rows) == 1 else "connections"
    conversation_word = "conversation" if len(connections) == 1 else "conversations"
    summary = f"{len(connections)} {conversation_word}  /  {len(rows)} {connection_word}"
    gap = max(2, width - len(summary) - len(_ANSI_SGR.sub("", receiver_text)))
    lines.append(summary + " " * gap + receiver_text)
    if detail:
        lines.append(
            "Status: " + " ".join(
                f"{_status_dot(label, color)} {description}" for label, description in (
                    ("ON", "enabled"), ("OFF", "paused"),
                    ("STALE", "stale"), ("UNKNOWN", "unavailable"),
                )
            )
        )
        lines.append("Scope: delivery + reported work · no live model/tool telemetry")
    if not rows:
        lines.append("  No bindings yet")
    if width >= 60:
        conversation_width = min(40, max(20, width // 2))
        connections_width = min(18, max(12, width // 6))
        lines.append(
            "      " + _fit("Conversation", conversation_width) +
            _fit("Connections", connections_width) + "Delivery events"
        )
        lines.append(_paint("─" * width, 90, color))
    previous_project = None
    for conversation_index, connection in enumerate(connections):
        project = connection.get("project") or "Ungrouped"
        if project != previous_project:
            if previous_project is not None:
                lines.append("")
            lines.append("  " + _paint(_safe(project), 1, color))
            previous_project = project
        bindings = connection.get("bindings") or []
        marker = _paint("▶", 36, color) if conversation_index == selected and not color else " "
        label = _conversation_label(connection)
        activity = _conversation_activity(connection)
        if width >= 60:
            bar = _paint("▌", 32, color) if conversation_index == selected else " "
            prefix = f"{bar} {marker} {_status_dot(_conversation_status(connection), color)} "
            label_display = _paint(label, 1, color)
            activity_width = max(1, width - 6 - conversation_width - connections_width)
            line = (
                prefix + _fit(label_display, conversation_width) +
                _fit(str(len(bindings)), connections_width) + _fit(activity, activity_width)
            )
        else:
            line = (
                f"{'▌' if conversation_index == selected else ' '} {marker} {_status_dot(_conversation_status(connection), color)} "
                f"{_paint(label, 1, color)} · {len(bindings)} · {activity}"
            )
        if conversation_index == selected and color:
            background = "\x1b[48;5;236m"
            line = background + line.replace("\x1b[0m", "\x1b[0m" + background) + "\x1b[0m"
        elif conversation_index != selected:
            line = line.rstrip()
        lines.append(line)
        if detail == "all":
            for binding in bindings:
                lines.append("")
                lines.extend(_detail_lines(connection, binding, color))
        elif detail and conversation_index == selected and bindings:
            route = selected_route % len(bindings)
            lines.append("")
            lines.extend(_detail_lines(connection, bindings[route], color))
    if snapshot.get("warnings"):
        lines.append("Warnings: " + "; ".join(_safe(value) for value in snapshot["warnings"]))
    return [_clip(line, width) for line in lines]


def _selection_context(snapshot: dict[str, Any], selected: int, selected_route: int,
                       width: int, color: bool) -> list[str]:
    """Render the persistent selected conversation and route context."""

    divider = _paint("─" * max(1, width), 90, color)
    connections = snapshot.get("connections") or []
    if selected < 0 or selected >= len(connections):
        return [divider, "  No conversation selected", "  Enter to open selected route", divider]
    connection = connections[selected]
    bindings = connection.get("bindings") or []
    if not bindings:
        return [divider, "  " + _conversation_label(connection), "  No routes available", divider]
    route = selected_route % len(bindings)
    binding = bindings[route]
    label = _safe(connection.get("project") or "Ungrouped") + " / " + _conversation_label(connection)
    route_label = _binding_label(connection, binding)
    route_line = f"  Route {route + 1}/{len(bindings)} · {route_label}"
    return [
        divider,
        "  " + _paint(label, 1, color),
        "  " + _conversation_summary(connection),
        route_line,
        "  Enter to open selected route",
        divider,
    ]


def render_text(snapshot: dict[str, Any], *, width: int = 100, height: int = 24, scroll: int = 0,
                color: bool = False, selected: int = 0, selected_route: int = 0,
                detail: bool | str = False, live: bool = False,
                frame: bool = False, animate: bool = True, now: float | None = None,
                notice: str | None = None, notice_kind: str = "OPEN") -> str:
    """Render a bounded viewport with a persistent header and compact table."""

    width = min(96, max(1, int(width or 1)))
    height = max(2, int(height or 2))
    body = render_lines(snapshot, width, color=color, selected=selected, selected_route=selected_route, detail=detail,
                        live=live, frame=frame, animate=animate, now=now)
    context = _selection_context(snapshot, selected, selected_route, width, color)
    # Keep the title, receiver state and summary pinned. The conversation list
    # scrolls while the selected context and controls stay visible.
    detail_header_count = 4 if detail and len(body) > 3 and body[3].startswith("Status:") else 3
    if detail and len(body) > 4 and body[4].startswith("Scope:"):
        detail_header_count = 5
    header_count = min(detail_header_count, len(body))
    header = body[:header_count]
    table = body[header_count:]
    if height >= 20:
        header.append("")
    if height < 16:
        # Keep route identity and exit keys visible even in very short terminals.
        context = [context[3]] if len(context) >= 6 else context[1:2]
    header_count = len(header)
    footer_lines = len(context) + (1 if notice else 0) + 1
    available = max(0, height - footer_lines)
    body_slots = max(0, available - header_count)
    maximum = max(0, len(table) - body_slots)
    offset = min(max(int(scroll), 0), maximum)
    # Selection is global across grouped conversation headers.  Keep the
    # highlighted row visible even when the table has many groups.
    selected_line = next(
        (index for index, line in enumerate(table) if "▌" in _ANSI_SGR.sub("", line) or "▶" in _ANSI_SGR.sub("", line)),
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
    conversation_total = len(snapshot.get("connections") or [])
    conversation_position = min(max(int(selected), 0), max(0, conversation_total - 1)) + 1 if conversation_total else 0
    footer = (
        f"↑↓ Tab · Enter open · p stop · r resume · x delete · d · q · "
        f"{conversation_position}/{conversation_total}"
    )
    if width >= 90:
        freshness = _refresh_line(snapshot, time.time() if now is None else now)
        if len(footer) + len(freshness) + 2 <= width:
            footer += " " * (width - len(footer) - len(freshness)) + freshness
    if width < 65:
        footer = "↑↓ Tab Enter p/r x d q"
    if width < 36:
        footer = "↑↓ Tab Enter p/r x d q"
    if height < 16:
        trailing = context + ([notice_kind + ": " + notice] if notice else []) + [footer]
    else:
        trailing = context[:-1] + ([notice_kind + ": " + notice] if notice else []) + [context[-1], footer]
    # Keep the whole panel together instead of stretching to the terminal edges.
    content = visible + trailing[:-1]
    return "\n".join(_clip(line, width) for line in (content + trailing[-1:])[-height:])


def _action_target(snapshot: dict[str, Any], selected: int, selected_route: int) -> dict[str, str]:
    index = _selected_binding_index(snapshot, selected, selected_route)
    connection, binding, _ = _binding_rows(snapshot)[index]
    if binding.get("identity_exact") is not True:
        raise DashboardError("route identity is not exact; inspect it before changing monitoring")
    return {"binding": binding["name"], "thread": connection["thread"], "endpoint": binding["endpoint"]}


def _apply_action(root: Path, target: dict[str, str], action: str) -> str:
    # Runtime construction is intentionally confined to explicit keyboard actions.
    from .monitor import Monitor, IngressError
    def no_session(*args, **kwargs):
        raise DashboardError("monitor controls must not create native sessions")
    try:
        Monitor(root, no_session).dashboard_action(**target, action=action)
    except (IngressError, ValueError, OSError, sqlite3.Error) as exc:
        raise DashboardError(str(exc)) from exc
    verb = {"pause": "Stopped", "resume": "Resumed", "remove": "Removed"}[action]
    return f"{verb} route {target['binding']}; native conversation kept"


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


def _launch_selected(
    reader: DashboardReader,
    snapshot: dict[str, Any],
    selected: int,
    suspend,
    restore,
    *,
    runner=subprocess.run,
) -> str:
    """Resume the exact selected saved conversation and return an honest status."""

    rows = _binding_rows(snapshot)
    if not rows:
        raise DashboardError("no conversation binding is selected")
    if selected < 0 or selected >= len(rows):
        raise DashboardError("selected conversation is out of range")
    connection, displayed_binding, _ = rows[selected]
    if displayed_binding.get("identity_exact") is not True:
        raise DashboardError("selected binding identity is unsafe; refresh the dashboard")
    binding = reader.selected_binding(
        displayed_binding.get("name"), connection.get("thread"), displayed_binding.get("endpoint")
    )
    command = ["codex", "--remote", binding["endpoint"], "resume", binding["thread"]]
    environment = os.environ.copy()
    try:
        token = server_token()
    except Exception as exc:
        raise DashboardError(f"cannot prepare remote authentication ({type(exc).__name__})") from exc
    if token is not None:
        environment["CODEX_MONITOR_SERVER_TOKEN"] = token
        # Keep --remote adjacent to its endpoint. Inserting options between
        # them makes the Codex CLI parse the auth flag as the endpoint.
        command[1:1] = ["--remote-auth-token-env", "CODEX_MONITOR_SERVER_TOKEN"]
    suspend()
    try:
        completed = runner(command, shell=False, env=environment, check=False)
    except OSError as exc:
        return f"open failed: {type(exc).__name__}"
    except KeyboardInterrupt:
        # SIGINT can interrupt wait() in this process after reaching the
        # foreground child. Restore cbreak/alternate-screen state and resume
        # the dashboard instead of treating it as a dashboard quit.
        return "Codex interrupted"
    finally:
        restore()
    return (
        "Codex exited successfully" if completed.returncode == 0
        else f"Codex exited with status {completed.returncode}"
    )


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
    fd = stdin.fileno()
    old = termios.tcgetattr(fd)
    scroll = 0
    selected = 0
    selected_route = 0
    detail = False
    open_status: str | None = None
    notice_kind = "OPEN"
    delete_target: dict[str, str] | None = None

    def suspend_dashboard() -> None:
        """Return the terminal to the user's shell state for the child TUI."""

        termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        stdout.write("\x1b[?25h\x1b[0m\x1b[?1049l")
        stdout.flush()

    def restore_dashboard() -> None:
        """Re-enter dashboard cbreak/alternate-screen state after the child."""

        # A child is free to leave the inherited terminal in raw mode or with
        # queued navigation bytes. Restore the dashboard's known baseline and
        # flush that input before applying cbreak again.
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        tty.setcbreak(fd)
        stdout.write("\x1b[?1049h\x1b[?25l")
        stdout.flush()

    try:
        tty.setcbreak(fd)
        stdout.write("\x1b[?1049h\x1b[?25l")
        # The first snapshot is synchronous so a healthy state appears
        # immediately.  Later reads run in a daemon worker; a slow status
        # probe therefore cannot freeze the 0.5 second live animation.
        snapshot = reader.snapshot()
        displayed_snapshot = snapshot
        initial_connections = snapshot.get("connections") or []
        if initial_connections:
            selected_route = _preferred_route(initial_connections[0])
        poll_lock = threading.Lock()
        poll_running = False
        pending_snapshot: dict[str, Any] | None = None
        inventory_epoch = 0

        def start_poll() -> None:
            nonlocal poll_running
            with poll_lock:
                if poll_running:
                    return
                poll_running = True
                epoch = inventory_epoch

            def poll() -> None:
                nonlocal poll_running, pending_snapshot
                try:
                    value = reader.snapshot()
                    with poll_lock:
                        if epoch == inventory_epoch:
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
                if pending_snapshot is not None and delete_target is None:
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
                connections = snapshot.get("connections") or []
                selected = min(max(selected, 0), max(0, len(connections) - 1))
                if connections and connections[selected].get("bindings"):
                    selected_route %= len(connections[selected]["bindings"])
                else:
                    selected_route = 0
                rendered = render_text(
                    snapshot, width=size.columns, height=size.lines, scroll=scroll,
                    color=color_enabled, selected=selected, selected_route=selected_route,
                    detail=detail, live=True,
                    frame=frame, animate=animate, notice=open_status, notice_kind=notice_kind,
                )
                _draw_frame(stdout, rendered)
                stdout.flush()
                # Keep activation tied to the inventory the user actually
                # saw. A poll may finish between this draw and the next key
                # read, but it must not retarget Enter/o.
                displayed_snapshot = snapshot
            wait = min(.1, max(0.0, next_frame - now_mono), max(0.0, next_poll - now_mono))
            ready, _, _ = select.select([stdin], [], [], wait)
            if not ready:
                continue
            value = _key(stdin)
            if value in ("q", "Q", "\x03"):
                return 0
            connections = displayed_snapshot.get("connections") or []
            if delete_target is not None:
                target = delete_target
                delete_target = None
                notice_kind = "MONITOR"
                if value in ("y", "Y"):
                    try:
                        open_status = _apply_action(reader.root, target, "remove")
                    except DashboardError as exc:
                        open_status = str(exc)
                    with poll_lock:
                        inventory_epoch += 1
                        pending_snapshot = None
                    snapshot = reader.snapshot()
                    next_poll = 0.0
                else:
                    open_status = "Deletion cancelled"
                next_frame = 0.0
                continue
            if value in ("p", "r", "x"):
                notice_kind = "MONITOR"
                try:
                    target = _action_target(displayed_snapshot, selected, selected_route)
                    if value == "x":
                        delete_target = target
                        notice_kind = "DELETE"
                        open_status = f"y confirm / other key cancel · {target['binding']}"
                    else:
                        open_status = _apply_action(reader.root, target, "pause" if value == "p" else "resume")
                        with poll_lock:
                            inventory_epoch += 1
                            pending_snapshot = None
                        snapshot = reader.snapshot()
                        next_poll = 0.0
                except DashboardError as exc:
                    open_status = str(exc)
                next_frame = 0.0
                continue
            if value in ("j", "down", "k", "up", "pageup", "pagedown", "home", "end", "\t"):
                open_status = None
                notice_kind = "OPEN"
            if value in ("j", "down"):
                selected = min(selected + 1, max(0, len(connections) - 1))
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value in ("k", "up"):
                selected = max(0, selected - 1)
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value == "pageup":
                selected = max(0, selected - max(1, size.lines - 8))
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value == "pagedown":
                selected = min(max(0, len(connections) - 1), selected + max(1, size.lines - 8))
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value == "home":
                selected = 0
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value == "end":
                selected = max(0, len(connections) - 1)
                selected_route = _preferred_route(connections[selected]) if connections else 0
            elif value == "\t":
                bindings = connections[selected].get("bindings") or [] if connections else []
                if bindings:
                    selected_route = _cycle_route(connections[selected], selected_route)
            elif value == "d":
                detail = not detail
            elif value in ("\r", "\n", "o", "O"):
                notice_kind = "OPEN"
                try:
                    open_status = _launch_selected(
                        reader, displayed_snapshot,
                        _selected_binding_index(displayed_snapshot, selected, selected_route),
                        suspend_dashboard, restore_dashboard,
                    )
                except DashboardError as exc:
                    open_status = str(exc)
                next_frame = 0.0
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
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        stdout.write("\x1b[?25h\x1b[0m\x1b[?1049l")
        stdout.flush()
