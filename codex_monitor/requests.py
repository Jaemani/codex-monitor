"""Durable, explicitly advanced request lifecycles and notification outbox.

This module stores business state separately from native delivery state.  A
request becomes complete only when an operator or worker explicitly records a
terminal transition; consuming a native event or acknowledging a reply never
changes the request state.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .errors import IngressError


REQUEST_STATES = frozenset({
    "received", "acknowledged", "in_progress", "completed", "failed", "cancelled", "expired",
})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired"})
NOTIFICATION_STATES = frozenset({"pending", "accepted"})
NOTIFY_ON = frozenset({"in_progress", "completed", "failed", "cancelled", "expired"})

MAX_IDENTIFIER_BYTES = 200
MAX_REQUEST_PAYLOAD_BYTES = 16 * 1024
MAX_UPDATE_BYTES = 4 * 1024
MAX_ERROR_BYTES = 1024
MAX_NOTIFICATION_BACKOFF_SECONDS = 60.0
DEFAULT_MAX_REQUESTS = 10_000
DEFAULT_MAX_UPDATES_PER_REQUEST = 64
DEFAULT_MAX_NOTIFICATIONS = 10_000
DEFAULT_LIST_LIMIT = 100

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$")


class RequestError(IngressError):
    """A request lifecycle validation, ownership, or concurrency error."""


def _fail(message: str, status: int = 400):
    raise RequestError(message, status)


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail(f"{label} must be a nonempty identifier")
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        _fail(f"{label} is too long")
    return value


def _optional_identifier(value, label: str):
    if value is None:
        return None
    return _identifier(value, label)


def _finite_number(value, label: str, *, allow_none: bool = False, minimum: float | None = None) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(f"{label} must be finite")
    value = float(value)
    if minimum is not None and value < minimum:
        _fail(f"{label} must be at least {minimum}")
    return value


def _finite_sum(left: float, right: float, label: str) -> float:
    value = left + right
    if not math.isfinite(value):
        _fail(f"{label} must be finite")
    return value


def _json_object(value, label: str, limit: int) -> str:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        _fail(f"{label} must contain finite JSON values")
    if len(encoded.encode("utf-8")) > limit:
        _fail(f"{label} exceeds {limit} bytes")
    return encoded


def _optional_json_object(value, label: str, limit: int) -> str | None:
    if value is None:
        return None
    return _json_object(value, label, limit)


def _decode_object(encoded: str, label: str):
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"stored {label} is invalid") from exc
    if not isinstance(value, dict):
        raise RequestError(f"stored {label} is not an object")
    return value


def _validate_update_id(value) -> str:
    return _identifier(value, "update id")


def _validate_revision(value) -> int:
    if type(value) is not int or value < 0:
        _fail("expected revision must be a nonnegative integer")
    return value


class RequestStore:
    """SQLite-backed request state, append-only transitions, and outbox."""

    def __init__(
        self,
        root,
        *,
        max_requests=DEFAULT_MAX_REQUESTS,
        max_updates_per_request=DEFAULT_MAX_UPDATES_PER_REQUEST,
        max_notifications=DEFAULT_MAX_NOTIFICATIONS,
        max_payload_bytes=MAX_REQUEST_PAYLOAD_BYTES,
        max_update_bytes=MAX_UPDATE_BYTES,
        clock=time.time,
    ):
        for value, label in (
            (max_requests, "max_requests"),
            (max_updates_per_request, "max_updates_per_request"),
            (max_notifications, "max_notifications"),
            (max_payload_bytes, "max_payload_bytes"),
            (max_update_bytes, "max_update_bytes"),
        ):
            if type(value) is not int or value <= 0:
                _fail(f"{label} must be a positive integer")
        if max_updates_per_request < 2:
            _fail("max_updates_per_request must leave room for a terminal expiry")
        if not callable(clock):
            _fail("clock must be callable")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "requests.sqlite3"
        self.max_requests = max_requests
        self.max_updates_per_request = max_updates_per_request
        self.max_notifications = max_notifications
        self.max_payload_bytes = max_payload_bytes
        self.max_update_bytes = max_update_bytes
        self.clock = clock
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE NOT NULL,
                    conversation_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    original_delivery_id TEXT NOT NULL,
                    original_binding TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    expires_at REAL,
                    expires_in REAL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    UNIQUE(conversation_id, source, request_key)
                );
                CREATE INDEX IF NOT EXISTS requests_scope
                    ON requests(conversation_id, source, seq);
                CREATE TABLE IF NOT EXISTS request_updates (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    previous_state TEXT NOT NULL,
                    state TEXT NOT NULL,
                    summary TEXT,
                    created REAL NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(request_id),
                    UNIQUE(request_id, update_id)
                );
                CREATE INDEX IF NOT EXISTS request_updates_request
                    ON request_updates(request_id, revision);
                CREATE TABLE IF NOT EXISTS request_notifications (
                    notification_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    update_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    original_binding TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_id TEXT UNIQUE NOT NULL,
                    envelope TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,
                    delivery_id TEXT,
                    error TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(request_id),
                    UNIQUE(request_id, update_id)
                );
                CREATE INDEX IF NOT EXISTS request_notifications_due
                    ON request_notifications(state, next_at, created);
                """
            )
            # Serialize legacy-column inspection and upgrades across receiver
            # or CLI processes that initialize the same state directory.
            db.execute("BEGIN IMMEDIATE")
            request_columns = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
            if "expires_in" not in request_columns:
                db.execute("ALTER TABLE requests ADD COLUMN expires_in REAL")
            notification_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(request_notifications)")
            }
            if "revision" not in notification_columns:
                db.execute("ALTER TABLE request_notifications ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE request_notifications SET revision=(
                    SELECT revision FROM request_updates
                    WHERE request_updates.request_id=request_notifications.request_id
                      AND request_updates.update_id=request_notifications.update_id
                )""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # Changing journal mode takes a schema lock and can return
            # ``database is locked`` before SQLite applies busy_timeout.
            # Retry that one startup pragma while another initializer runs.
            db.execute("PRAGMA busy_timeout=10000")
            for _ in range(200):
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    time.sleep(.05)
            else:
                raise sqlite3.OperationalError("database is locked while selecting WAL mode")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        value = self.clock()
        return _finite_number(value, "clock value")

    @staticmethod
    def _request_row(db, *, request_id=None, conversation_id=None, source=None, request_key=None):
        if request_id is not None:
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM requests WHERE conversation_id=? AND source=? AND request_key=?",
                (conversation_id, source, request_key),
            ).fetchone()
        return row

    @staticmethod
    def _not_found():
        _fail("request not found", 404)

    def _record(self, db, row, *, duplicate=None, update_id=None):
        value = {
            "request_id": row["request_id"],
            "conversation_id": row["conversation_id"],
            "source": row["source"],
            "request_key": row["request_key"],
            "original_delivery_id": row["original_delivery_id"],
            "original_binding": row["original_binding"],
            "payload": _decode_object(row["payload"], "request payload"),
            "state": row["state"],
            "revision": row["revision"],
            "expires_at": row["expires_at"],
            "expires_in": row["expires_in"],
            "created": row["created"],
            "updated": row["updated"],
        }
        notifications = db.execute(
            """SELECT notification_id,update_id,original_binding,source,event_id,state,
                      attempts,next_at,delivery_id,error,created,updated
               FROM request_notifications WHERE request_id=? ORDER BY created""",
            (row["request_id"],),
        ).fetchall()
        value["notifications"] = [dict(item) for item in notifications]
        updates_used = db.execute(
            "SELECT count(*) FROM request_updates WHERE request_id=?", (row["request_id"],)
        ).fetchone()[0]
        notifications_used = self._pending_notification_count(db)
        notifications_total = db.execute("SELECT count(*) FROM request_notifications").fetchone()[0]
        open_requests = db.execute(
            "SELECT count(*) FROM requests WHERE state NOT IN ('completed','failed','cancelled','expired')",
        ).fetchone()[0]
        value["capacity"] = {
            "updates_used": updates_used,
            "updates_limit": self.max_updates_per_request,
            "updates_remaining": max(0, self.max_updates_per_request - updates_used),
            "notifications_used": notifications_used,
            "notifications_total": notifications_total,
            "notifications_limit": self.max_notifications,
            "notifications_remaining": max(0, self.max_notifications - notifications_used),
            "open_requests": open_requests,
            "notification_reservations": open_requests,
        }
        if duplicate is not None:
            value["duplicate"] = bool(duplicate)
        if update_id is not None:
            value["update_id"] = update_id
        return value

    @staticmethod
    def _summary(encoded):
        return None if encoded is None else _decode_object(encoded, "transition summary")

    def _check_scope(self, row, conversation_id=None, source=None):
        if row is None:
            self._not_found()
        if conversation_id is not None and row["conversation_id"] != conversation_id:
            self._not_found()
        if source is not None and row["source"] != source:
            self._not_found()

    def create(
        self,
        conversation_id,
        source,
        request_key,
        original_delivery_id,
        original_binding,
        payload,
        *,
        expires_at=None,
        expires_in=None,
    ):
        conversation_id = _identifier(conversation_id, "conversation id")
        source = _identifier(source, "source")
        request_key = _identifier(request_key, "request key")
        original_delivery_id = _identifier(original_delivery_id, "original delivery id")
        original_binding = _identifier(original_binding, "original binding")
        payload_json = _json_object(payload, "request payload", self.max_payload_bytes)
        expires_at = _finite_number(expires_at, "expiry", allow_none=True, minimum=0)
        if expires_at is not None and expires_in is not None:
            _fail("expiry and expires_in are mutually exclusive")
        expires_in = _finite_number(expires_in, "relative expiry", allow_none=True, minimum=0)
        now = self._now()
        if expires_in is not None:
            expires_at = _finite_sum(now, expires_in, "relative expiry")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._request_row(
                db, conversation_id=conversation_id, source=source, request_key=request_key,
            )
            if existing is not None:
                if (
                    existing["original_delivery_id"] != original_delivery_id
                    or existing["original_binding"] != original_binding
                    or existing["payload"] != payload_json
                    or (
                        existing["expires_in"] != expires_in
                        if expires_in is not None or existing["expires_in"] is not None
                        else existing["expires_at"] != expires_at
                    )
                ):
                    _fail("request key already belongs to different immutable content", 409)
                return self._record(db, existing, duplicate=True)
            if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= self.max_requests:
                _fail("request capacity reached", 429)
            pending = self._pending_notification_count(db)
            open_requests = db.execute(
                "SELECT count(*) FROM requests WHERE state NOT IN ('completed','failed','cancelled','expired')",
            ).fetchone()[0]
            if pending + open_requests + 1 > self.max_notifications:
                _fail("request notification capacity reserved for open requests", 429)
            request_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO requests
                   (request_id,conversation_id,source,request_key,original_delivery_id,
                    original_binding,payload,state,revision,expires_at,expires_in,created,updated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, conversation_id, source, request_key, original_delivery_id,
                 original_binding, payload_json, "received", 0, expires_at, expires_in, now, now),
            )
            row = self._request_row(db, request_id=request_id)
            return self._record(db, row, duplicate=False)

    def get_by_id(self, request_id, *, conversation_id=None, source=None):
        request_id = _identifier(request_id, "request id")
        conversation_id = _optional_identifier(conversation_id, "conversation id")
        source = _optional_identifier(source, "source")
        with self.connect() as db:
            row = self._request_row(db, request_id=request_id)
            self._check_scope(row, conversation_id, source)
            return self._record(db, row)

    def create_for_parent(
        self, conversation_id, parent, request_key, payload, *, expires_at=None, expires_in=None,
    ):
        """Create from a parent delivery already resolved by the owner.

        The monitor/HTTP wrapper must resolve the delivery first and pass its
        complete receipt here.  This method deliberately accepts no free-form
        binding, source, or delivery ID, so a caller cannot silently attach a
        request to another conversation's receipt.
        """
        if not isinstance(parent, dict):
            _fail("known parent delivery receipt is required")
        required = ("id", "binding", "source")
        if any(key not in parent for key in required):
            _fail("known parent delivery receipt is required")
        parent_conversation = parent.get("conversation_id", parent.get("thread"))
        if parent_conversation is not None and parent_conversation != conversation_id:
            _fail("known parent delivery belongs to another conversation", 404)
        return self.create(
            conversation_id,
            parent["source"],
            request_key,
            parent["id"],
            parent["binding"],
            payload,
            expires_at=expires_at,
            expires_in=expires_in,
        )

    def get(self, conversation_id, source, request_key):
        conversation_id = _identifier(conversation_id, "conversation id")
        source = _identifier(source, "source")
        request_key = _identifier(request_key, "request key")
        with self.connect() as db:
            row = self._request_row(
                db, conversation_id=conversation_id, source=source, request_key=request_key,
            )
            self._check_scope(row)
            return self._record(db, row)

    def list_requests(self, conversation_id, *, source=None, limit=DEFAULT_LIST_LIMIT, after=None):
        conversation_id = _identifier(conversation_id, "conversation id")
        source = _optional_identifier(source, "source")
        if type(limit) is not int or not 1 <= limit <= DEFAULT_LIST_LIMIT:
            _fail(f"request limit must be 1..{DEFAULT_LIST_LIMIT}")
        if after is None:
            cursor = 0
        else:
            if isinstance(after, bool) or not isinstance(after, (str, int)):
                _fail("request cursor must be a nonnegative integer")
            try:
                cursor = int(after)
            except (TypeError, ValueError):
                _fail("request cursor must be a nonnegative integer")
            if cursor < 0:
                _fail("request cursor must be a nonnegative integer")
        with self.connect() as db:
            if source is None:
                rows = db.execute(
                    "SELECT * FROM requests WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (conversation_id, cursor, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT * FROM requests
                       WHERE conversation_id=? AND source=? AND seq>? ORDER BY seq LIMIT ?""",
                    (conversation_id, source, cursor, limit + 1),
                ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            data = [self._record(db, row) for row in rows]
            next_cursor = str(rows[-1]["seq"]) if has_more and rows else None
        return {"data": data, "next": next_cursor}

    def history(self, conversation_id, source, request_key):
        request = self.get(conversation_id, source, request_key)
        with self.connect() as db:
            rows = db.execute(
                """SELECT update_id,request_id,expected_revision,revision,previous_state,
                          state,summary,created FROM request_updates
                   WHERE request_id=? ORDER BY revision""",
                (request["request_id"],),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in (
                    "update_id", "request_id", "expected_revision", "revision",
                    "previous_state", "state", "created",
                )},
                "summary": self._summary(row["summary"]),
            }
            for row in rows
        ]

    @staticmethod
    def _allowed(previous, target):
        if previous in TERMINAL_STATES or target == previous:
            return False
        if target == "acknowledged":
            return previous == "received"
        if target == "in_progress":
            return previous in {"received", "acknowledged"}
        if target in TERMINAL_STATES:
            return previous not in TERMINAL_STATES
        return False

    def _pending_notification_count(self, db):
        return db.execute(
            "SELECT count(*) FROM request_notifications WHERE state='pending'",
        ).fetchone()[0]

    def _notification_capacity(self, db, row, target_state):
        if target_state not in NOTIFY_ON:
            return
        pending = self._pending_notification_count(db)
        if target_state in TERMINAL_STATES:
            # The request's open reservation is released by this transition.
            if pending + 1 > self.max_notifications:
                _fail("request notification capacity reached", 429)
            return
        open_requests = db.execute(
            "SELECT count(*) FROM requests WHERE state NOT IN ('completed','failed','cancelled','expired')",
        ).fetchone()[0]
        if pending + open_requests + 1 > self.max_notifications:
            _fail("request notification capacity reserved for open requests", 429)

    def _insert_notification(self, db, row, update_id, state, summary_json, now):
        if state not in NOTIFY_ON:
            return None
        if self._pending_notification_count(db) >= self.max_notifications:
            _fail("request notification capacity reached", 429)
        notification_id = f"request-notification-{row['request_id']}-{row['revision']}"
        event_id = f"request-{row['request_id']}-{row['revision']}"
        data = {
            "request_id": row["request_id"],
            "conversation_id": row["conversation_id"],
            "request_key": row["request_key"],
            "original_delivery_id": row["original_delivery_id"],
            "state": state,
            "revision": row["revision"],
            "message": f"Request {row['request_key']}: {state} (revision {row['revision']}).",
        }
        if summary_json is not None:
            data["summary"] = _decode_object(summary_json, "transition summary")
        envelope = {
            "id": event_id,
            "source": row["source"],
            "type": "request.status.changed",
            "data": data,
        }
        envelope_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
        db.execute(
            """INSERT INTO request_notifications
               (notification_id,request_id,update_id,revision,original_binding,source,event_id,
                envelope,state,attempts,next_at,created,updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (notification_id, row["request_id"], update_id, row["revision"],
             row["original_binding"], row["source"], event_id, envelope_json,
             "pending", 0, now, now, now),
        )
        return notification_id

    def _transition_db(
        self, db, row, update_id, target_state, expected_revision, summary_json, now,
    ):
        existing = db.execute(
            "SELECT * FROM request_updates WHERE request_id=? AND update_id=?",
            (row["request_id"], update_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["request_id"] != row["request_id"]
                or existing["expected_revision"] != expected_revision
                or existing["state"] != target_state
                or existing["summary"] != summary_json
            ):
                _fail("update id already used for different content", 409)
            return self._record(db, row, duplicate=True, update_id=update_id)
        if row["revision"] != expected_revision:
            _fail("request revision conflict", 409)
        if target_state not in REQUEST_STATES:
            _fail("unknown request state")
        if not self._allowed(row["state"], target_state):
            _fail("request state transition is not allowed", 409)
        self._notification_capacity(db, row, target_state)
        update_count = db.execute(
            "SELECT count(*) FROM request_updates WHERE request_id=?", (row["request_id"],)
        ).fetchone()[0]
        # Keep one transition slot available for the supervisor's explicit
        # expiry transition. Terminal transitions consume the reserved slot.
        if target_state not in TERMINAL_STATES and update_count + 1 >= self.max_updates_per_request:
            _fail("request update capacity reached; terminal expiry slot is reserved", 429)
        revision = expected_revision + 1
        db.execute(
            """INSERT INTO request_updates
               (update_id,request_id,expected_revision,revision,previous_state,state,summary,created)
               VALUES(?,?,?,?,?,?,?,?)""",
            (update_id, row["request_id"], expected_revision, revision, row["state"],
             target_state, summary_json, now),
        )
        db.execute(
            """UPDATE requests SET state=?,revision=?,updated=? WHERE request_id=?
               AND revision=?""",
            (target_state, revision, now, row["request_id"], expected_revision),
        )
        updated = self._request_row(db, request_id=row["request_id"])
        self._insert_notification(db, updated, update_id, target_state, summary_json, now)
        return self._record(db, updated, duplicate=False, update_id=update_id)

    def transition(
        self,
        conversation_id,
        source,
        request_key,
        *,
        update_id,
        target_state,
        expected_revision,
        summary=None,
    ):
        conversation_id = _identifier(conversation_id, "conversation id")
        source = _identifier(source, "source")
        request_key = _identifier(request_key, "request key")
        update_id = _validate_update_id(update_id)
        target_state = _identifier(target_state, "request state")
        expected_revision = _validate_revision(expected_revision)
        summary_json = _optional_json_object(summary, "transition summary", self.max_update_bytes)
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._request_row(
                db, conversation_id=conversation_id, source=source, request_key=request_key,
            )
            self._check_scope(row)
            return self._transition_db(db, row, update_id, target_state, expected_revision, summary_json, now)

    def expire_due(self, now=None, limit=DEFAULT_LIST_LIMIT):
        if now is None:
            now = self._now()
        else:
            now = _finite_number(now, "expiry time")
        if type(limit) is not int or not 1 <= limit <= DEFAULT_LIST_LIMIT:
            _fail(f"expiry limit must be 1..{DEFAULT_LIST_LIMIT}")
        results = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM requests
                   WHERE expires_at IS NOT NULL AND expires_at<=?
                     AND state NOT IN ('completed','failed','cancelled','expired')
                   ORDER BY seq LIMIT ?""",
                (now, limit),
            ).fetchall()
            for row in rows:
                # Keep the expiry transition available even when the bounded
                # notification outbox is full.  The supervisor can retry
                # expiry after an operator drains notifications.
                if self._pending_notification_count(db) >= self.max_notifications:
                    break
                update_id = f"request-expiry-{row['request_id']}-{row['revision']}"
                results.append(self._transition_db(
                    db, row, update_id, "expired", row["revision"], None, now,
                ))
        return results

    @staticmethod
    def _notification_row(db, notification_id):
        return db.execute(
            "SELECT * FROM request_notifications WHERE notification_id=?", (notification_id,)
        ).fetchone()

    @staticmethod
    def _notification_value(row):
        return {
            "notification_id": row["notification_id"],
            "request_id": row["request_id"],
            "update_id": row["update_id"],
            "original_binding": row["original_binding"],
            "source": row["source"],
            "event_id": row["event_id"],
            "envelope": json.loads(row["envelope"]),
            "state": row["state"],
            "attempts": row["attempts"],
            "next_at": row["next_at"],
            "delivery_id": row["delivery_id"],
            "error": row["error"],
            "created": row["created"],
            "updated": row["updated"],
        }

    def pending_notifications(self, limit=DEFAULT_LIST_LIMIT, *, now=None):
        if type(limit) is not int or not 1 <= limit <= DEFAULT_LIST_LIMIT:
            _fail(f"notification limit must be 1..{DEFAULT_LIST_LIMIT}")
        if now is None:
            now = self._now()
        else:
            now = _finite_number(now, "notification time")
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM request_notifications AS current
                   WHERE current.state='pending' AND current.next_at<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM request_notifications AS earlier
                         WHERE earlier.request_id=current.request_id
                           AND earlier.revision<current.revision
                           AND earlier.state<>'accepted'
                     )
                   ORDER BY next_at,created LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [self._notification_value(row) for row in rows]

    def notification_accepted(self, notification_id, delivery_id):
        notification_id = _identifier(notification_id, "notification id")
        delivery_id = _identifier(delivery_id, "delivery id")
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._notification_row(db, notification_id)
            if row is None:
                _fail("notification not found", 404)
            if row["state"] == "accepted":
                if row["delivery_id"] != delivery_id:
                    _fail("notification already accepted with a different delivery", 409)
                return {**self._notification_value(row), "duplicate": True}
            db.execute(
                """UPDATE request_notifications SET state='accepted',delivery_id=?,error=NULL,updated=?
                   WHERE notification_id=? AND state='pending'""",
                (delivery_id, now, notification_id),
            )
            row = self._notification_row(db, notification_id)
            return {**self._notification_value(row), "duplicate": False}

    def notification_error(self, notification_id, error, *, retry_after=None):
        notification_id = _identifier(notification_id, "notification id")
        if not isinstance(error, str) or not error.strip():
            _fail("notification error must be nonempty")
        if len(error.encode("utf-8")) > MAX_ERROR_BYTES:
            _fail(f"notification error exceeds {MAX_ERROR_BYTES} bytes")
        if retry_after is not None:
            retry_after = _finite_number(retry_after, "notification retry delay", minimum=0)
            retry_after = min(retry_after, MAX_NOTIFICATION_BACKOFF_SECONDS)
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._notification_row(db, notification_id)
            if row is None:
                _fail("notification not found", 404)
            if row["state"] == "accepted":
                _fail("accepted notification cannot be failed", 409)
            attempts = row["attempts"] + 1
            delay = retry_after if retry_after is not None else min(
                MAX_NOTIFICATION_BACKOFF_SECONDS, 2 ** min(attempts - 1, 6),
            )
            db.execute(
                """UPDATE request_notifications
                   SET attempts=?,next_at=?,error=?,updated=? WHERE notification_id=? AND state='pending'""",
                (attempts, _finite_sum(now, delay, "notification retry time"), error, now, notification_id),
            )
            row = self._notification_row(db, notification_id)
            return {**self._notification_value(row), "duplicate": False}

    def notification_deferred(self, notification_id, reason, *, retry_after=1.0):
        """Leave a pending notification quiet without consuming a retry."""
        notification_id = _identifier(notification_id, "notification id")
        if not isinstance(reason, str) or not reason.strip():
            _fail("notification defer reason must be nonempty")
        if len(reason.encode("utf-8")) > MAX_ERROR_BYTES:
            _fail(f"notification defer reason exceeds {MAX_ERROR_BYTES} bytes")
        retry_after = _finite_number(retry_after, "notification defer delay", minimum=0)
        retry_after = min(retry_after, MAX_NOTIFICATION_BACKOFF_SECONDS)
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._notification_row(db, notification_id)
            if row is None:
                _fail("notification not found", 404)
            if row["state"] == "accepted":
                _fail("accepted notification cannot be deferred", 409)
            db.execute(
                """UPDATE request_notifications SET next_at=?,error=?,updated=?
                   WHERE notification_id=? AND state='pending'""",
                (_finite_sum(now, retry_after, "notification defer time"), reason, now, notification_id),
            )
            row = self._notification_row(db, notification_id)
            return {**self._notification_value(row), "duplicate": False}
