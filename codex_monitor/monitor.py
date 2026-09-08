"""Durable event inbox. Codex owns conversation scheduling and user approvals."""
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import re
from contextlib import contextmanager
from urllib.parse import urlsplit

from .errors import IngressError, Retryable, Uncertain, Permanent
from .conditions import ConditionDebouncer
from .lock import process_alive
from .predicates import PredicateError, normalize_condition, public_condition
from .presentation import render_event

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$")
MANAGED_SOURCE = "managed/file"
MAX_MANAGED_WATCHES = 128
MAX_MANAGED_WATCHES_PER_THREAD = 32
MANAGED_LIVENESS_WINDOW = 2.0
_SSH_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_endpoint(endpoint):
    """Validate an endpoint without opening a network or App Server connection."""
    if not isinstance(endpoint, str) or not endpoint or "\x00" in endpoint:
        raise IngressError("endpoint must be a valid local or App Server transport")
    if endpoint in ("shared-local", "local", "unix://"):
        return endpoint
    if endpoint.startswith("unix://"):
        path = endpoint.removeprefix("unix://")
        if not path.startswith("/") or "\x00" in path:
            raise IngressError("Unix endpoint must use an absolute socket path")
        return endpoint
    if endpoint.startswith(("ws://", "wss://")):
        if any(char.isspace() for char in endpoint):
            raise IngressError("endpoint URL is malformed")
        try:
            parsed = urlsplit(endpoint)
            hostname = parsed.hostname
            parsed.port  # Force malformed and out-of-range ports to fail here.
        except ValueError as exc:
            raise IngressError("endpoint URL is malformed") from exc
        if (parsed.scheme not in ("ws", "wss") or not parsed.netloc or parsed.netloc.endswith(":") or
                not hostname or
                parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise IngressError("endpoint URL is malformed or contains credentials")
        if parsed.scheme == "ws" and hostname.lower() not in ("127.0.0.1", "localhost", "::1"):
            raise IngressError("ws endpoint must use a loopback host; use wss:// for remote hosts")
        return endpoint
    if endpoint.startswith("ssh://") and _SSH_ALIAS.fullmatch(endpoint.removeprefix("ssh://")):
        return endpoint
    raise IngressError("endpoint must be shared-local, local, unix:///absolute/path, ssh://ALIAS, ws://loopback, or wss://")


class Monitor:
    def __init__(self, state_dir, session_factory, clock=time.time, *, max_attempts=5, max_age=3600,
                 max_pending=1000, rate_limit=120, trace_limit=16):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "monitor.sqlite3"
        self.factory = session_factory
        self.clock = clock
        self.max_attempts = max_attempts
        self.max_age = max_age
        self.max_pending = max_pending
        self.rate_limit = rate_limit
        self.trace_limit = trace_limit
        if any(type(x) is not int or x <= 0 for x in (max_attempts, max_age, max_pending, rate_limit, trace_limit)):
            raise ValueError("all limits must be positive integers")
        self.wakeup = threading.Event()
        self.dispatch_lock = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bindings (
                    name TEXT PRIMARY KEY, thread TEXT NOT NULL, endpoint TEXT NOT NULL,
                    sources TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    binding TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
                    envelope TEXT NOT NULL, client_id TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                    submission_id TEXT, error TEXT,
                    UNIQUE(binding, source, event_id));
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL, action TEXT NOT NULL,
                    reason TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS event_dispatch ON events(state,next_at,seq);
                CREATE INDEX IF NOT EXISTS event_binding_order ON events(binding,state,seq);
                CREATE INDEX IF NOT EXISTS event_binding_rate ON events(binding,created);
                CREATE TABLE IF NOT EXISTS managed_watches (
                    id TEXT PRIMARY KEY, thread TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL, interval REAL NOT NULL, binding TEXT UNIQUE NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, removed INTEGER NOT NULL DEFAULT 0,
                    worker_state TEXT NOT NULL DEFAULT 'stopped', last_error TEXT,
                    last_sample_error TEXT, last_delivery_id TEXT, worker_seen REAL,
                    created REAL NOT NULL, updated REAL NOT NULL, condition_json TEXT);
                CREATE INDEX IF NOT EXISTS managed_active ON managed_watches(thread,name,removed);
            """)
            # Serialize schema inspection and migrations across receiver/CLI startup.
            db.execute("BEGIN IMMEDIATE")
            managed_columns = {row["name"] for row in db.execute("PRAGMA table_info(managed_watches)")}
            if "last_sample_error" not in managed_columns:
                db.execute("ALTER TABLE managed_watches ADD COLUMN last_sample_error TEXT")
            if "worker_seen" not in managed_columns:
                db.execute("ALTER TABLE managed_watches ADD COLUMN worker_seen REAL")
            if "lifecycle_epoch" not in managed_columns:
                db.execute("ALTER TABLE managed_watches ADD COLUMN lifecycle_epoch INTEGER NOT NULL DEFAULT 0")
            if "debounce_seconds" not in managed_columns:
                db.execute("ALTER TABLE managed_watches ADD COLUMN debounce_seconds REAL NOT NULL DEFAULT 0")
            if "condition_json" not in managed_columns:
                db.execute("ALTER TABLE managed_watches ADD COLUMN condition_json TEXT")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, name, thread, endpoint, sources):
        if MANAGED_SOURCE in sources:
            raise IngressError("reserved managed source cannot be registered externally", 403)
        if not all(isinstance(x, str) and NAME.fullmatch(x) for x in [name, thread, *sources]) or not sources or "/" in name:
            raise IngressError("binding, thread and sources must be nonempty identifiers")
        with self.connect() as db:
            db.execute("INSERT INTO bindings(name,thread,endpoint,sources) VALUES(?,?,?,?)",
                       (name, thread, endpoint, compact(sources)))
        return {"name": name, "thread": thread, "endpoint": endpoint, "sources": sources}

    @staticmethod
    def _managed_checkpoint(root, watch_id):
        return root / "managed" / (watch_id + ".json")

    @staticmethod
    def _managed_condition_checkpoint(root, watch_id):
        return root / "managed" / (watch_id + ".condition.json")

    @staticmethod
    def _managed_validate(thread, name, path, interval):
        if not isinstance(thread, str) or not NAME.fullmatch(thread):
            raise IngressError("thread must be a nonempty identifier")
        if not isinstance(name, str) or not NAME.fullmatch(name) or "/" in name:
            raise IngressError("watch name must be a nonempty identifier without '/'")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise IngressError("managed file path must be absolute")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not .1 <= interval <= 86400:
            raise IngressError("managed watch interval must be between 0.1 and 86400 seconds")

    def managed_create(
        self, thread, name, path, interval=2.0, endpoint="shared-local", *,
        debounce_seconds=0, condition=None,
    ):
        endpoint = validate_endpoint(endpoint)
        self._managed_validate(thread, name, path, interval)
        if isinstance(debounce_seconds, bool) or not isinstance(debounce_seconds, (int, float)) or not math.isfinite(debounce_seconds) or not 0 <= debounce_seconds <= 86400:
            raise IngressError("debounce seconds must be between 0 and 86400")
        if condition is None:
            condition_json = None
        else:
            try:
                _, condition_json = normalize_condition(condition)
            except PredicateError as exc:
                raise IngressError(str(exc)) from exc
        watch_id = uuid.uuid4().hex
        binding = "managed-" + watch_id
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM managed_watches WHERE thread=? AND name=? AND removed=0", (thread, name)).fetchone():
                raise IngressError("a managed watch with this name already exists for the thread", 409)
            if db.execute("SELECT count(*) FROM managed_watches WHERE removed=0").fetchone()[0] >= MAX_MANAGED_WATCHES:
                raise IngressError("managed watch capacity reached", 429)
            if db.execute("SELECT count(*) FROM managed_watches WHERE thread=? AND removed=0", (thread,)).fetchone()[0] >= MAX_MANAGED_WATCHES_PER_THREAD:
                raise IngressError("managed watch capacity for this thread reached", 429)
            db.execute("INSERT INTO bindings(name,thread,endpoint,sources) VALUES(?,?,?,?)",
                       (binding, thread, endpoint, compact([MANAGED_SOURCE])))
            db.execute("""INSERT INTO managed_watches
                (id,thread,name,path,interval,binding,created,updated,debounce_seconds,condition_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (watch_id, thread, name, path, float(interval), binding, now, now,
                        float(debounce_seconds), condition_json))
        self._managed_checkpoint(self.root, watch_id).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.wakeup.set()
        return self.managed_status(thread, name)

    def _managed_row(self, thread, name, *, include_removed=False):
        query = "SELECT * FROM managed_watches WHERE thread=? AND name=?"
        params = [thread, name]
        if not include_removed:
            query += " AND removed=0"
        query += " ORDER BY created DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        if row is None:
            raise IngressError("unknown managed watch", 404)
        return dict(row)

    def managed_runtime_rows(self, limit=MAX_MANAGED_WATCHES):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM managed_watches WHERE removed=0 ORDER BY created LIMIT ?", (limit,))]

    def managed_runtime_row(self, watch_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM managed_watches WHERE id=?", (watch_id,)).fetchone()
        return dict(row) if row else None

    def managed_set_worker(self, watch_id, state, error=None, last_delivery_id=None, sample_error=None):
        with self.connect() as db:
            now = self.clock()
            if state == "running":
                if last_delivery_id is not None:
                    # A delivery that completed just before pause/remove is
                    # still recorded, while the disabled generation stays
                    # stopped and cannot be revived by a late sampler write.
                    db.execute("UPDATE managed_watches SET last_delivery_id=?,updated=? WHERE id=?",
                               (last_delivery_id, now, watch_id))
                db.execute("""UPDATE managed_watches SET worker_state=?,last_error=?,last_sample_error=?,
                             worker_seen=?,updated=? WHERE id=? AND removed=0 AND enabled=1""",
                           (state, error, sample_error, now, now, watch_id))
                return
            if state == "stopped" and error is None and last_delivery_id is None:
                db.execute("UPDATE managed_watches SET worker_state=?,worker_seen=NULL,updated=? WHERE id=?",
                           (state, now, watch_id))
                return
            if last_delivery_id is None:
                db.execute("""UPDATE managed_watches SET worker_state=?,last_error=?,last_sample_error=?,
                             worker_seen=?,updated=? WHERE id=?""",
                           (state, error, sample_error, now if state == "running" else None, now, watch_id))
            else:
                db.execute("""UPDATE managed_watches SET worker_state=?,last_error=?,last_sample_error=?,
                             last_delivery_id=?,worker_seen=?,updated=? WHERE id=?""",
                           (state, error, sample_error, last_delivery_id, now if state == "running" else None, now, watch_id))

    def managed_heartbeat(self, watch_id):
        with self.connect() as db:
            db.execute("""UPDATE managed_watches SET worker_state='running',worker_seen=?,updated=?
                         WHERE id=? AND removed=0 AND enabled=1""", (self.clock(), self.clock(), watch_id))

    def _managed_checkpoint_state(self, watch_id):
        path = self._managed_checkpoint(self.root, watch_id)
        if not path.exists():
            return None, None
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict) or "last" not in state or "pending" not in state:
                raise ValueError("checkpoint must contain last and pending")
            return state, None
        except (OSError, ValueError, TypeError) as exc:
            return None, str(exc)[:500]

    def _managed_public(self, row):
        checkpoint, checkpoint_error = self._managed_checkpoint_state(row["id"])
        with self.connect() as db:
            binding = db.execute("SELECT endpoint FROM bindings WHERE name=?", (row["binding"],)).fetchone()
        endpoint = binding["endpoint"] if binding is not None else None
        predicate = None
        condition_error = None
        if row.get("condition_json"):
            try:
                predicate = public_condition(row["condition_json"])
            except PredicateError as exc:
                condition_error = str(exc)[:500]
        condition = None
        if row["debounce_seconds"] or row.get("condition_json"):
            try:
                condition = ConditionDebouncer(self._managed_condition_checkpoint(self.root, row["id"]), row["debounce_seconds"]).status()
            except (OSError, ValueError) as exc:
                condition_error = str(exc)[:500]
        last_delivery = None
        if row["last_delivery_id"]:
            with self.connect() as db:
                event = db.execute("SELECT id,state,updated,submission_id FROM events WHERE id=?",
                                   (row["last_delivery_id"],)).fetchone()
            if event:
                last_delivery = {**dict(event), "delivery_id": event["id"],
                                 "target_client": "unknown", "target_verification": "not_checked"}
            else:
                last_delivery = {"delivery_id": row["last_delivery_id"], "state": "unknown",
                                  "target_client": "unknown", "target_verification": "not_checked"}
        receiver_running = process_alive(self.root / "serve.lock")
        worker_fresh = (row["worker_state"] == "running" and row["worker_seen"] is not None and
                        self.clock() - row["worker_seen"] <= MANAGED_LIVENESS_WINDOW)
        if not row["enabled"] or row["removed"]:
            collector_status = "stopped"
        else:
            collector_status = "running" if receiver_running and worker_fresh else (
                "stale" if row["worker_state"] == "running" else row["worker_state"])
        return {
            "id": row["id"], "thread": row["thread"], "name": row["name"], "file": row["path"],
            "interval": row["interval"], "debounce_seconds": row["debounce_seconds"],
            "binding": row["binding"], "endpoint": endpoint, "enabled": bool(row["enabled"]),
            "predicate": predicate,
            "collector_status": collector_status, "last_sample": checkpoint.get("last") if checkpoint else None,
            "checkpoint_pending": bool(checkpoint and checkpoint.get("pending")),
            "checkpoint_error": checkpoint_error, "condition": condition, "condition_error": condition_error,
            "last_error": row["last_error"],
            "last_sample_error": row["last_sample_error"],
            "last_delivery": last_delivery, "receiver_running": receiver_running,
            "target_client": "unknown", "target_verification": "not_checked",
        }

    def managed_status(self, thread, name=None):
        if not isinstance(thread, str) or not NAME.fullmatch(thread):
            raise IngressError("thread must be a nonempty identifier")
        if name is None:
            with self.connect() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM managed_watches WHERE thread=? AND removed=0 ORDER BY name", (thread,))]
            return {"thread": thread, "monitors": [self._managed_public(row) for row in rows]}
        return self._managed_public(self._managed_row(thread, name))

    def managed_set_enabled(self, thread, name, enabled):
        row = self._managed_row(thread, name)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM managed_watches WHERE id=? AND removed=0", (row["id"],)).fetchone()
            if current is None:
                raise IngressError("unknown managed watch", 404)
            db.execute("UPDATE managed_watches SET enabled=?,worker_state=?,updated=?,lifecycle_epoch=lifecycle_epoch+1 WHERE id=?",
                       (int(enabled), "starting" if enabled else "stopped", self.clock(), row["id"]))
            db.execute("UPDATE bindings SET enabled=? WHERE name=?", (int(enabled), row["binding"]))
        self.wakeup.set()
        return self.managed_status(thread, name)

    def managed_remove(self, thread, name):
        row = self._managed_row(thread, name)
        result = self._managed_public(row)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE managed_watches SET enabled=0,removed=1,worker_state='stopped',updated=?,lifecycle_epoch=lifecycle_epoch+1 WHERE id=?",
                       (self.clock(), row["id"]))
            db.execute("UPDATE bindings SET enabled=0 WHERE name=?", (row["binding"],))
        self.wakeup.set()
        result.update({"enabled": False, "collector_status": "stopped", "removed": True,
                       "note": "Existing receipts and checkpoint are preserved; recreating the name creates a new generation."})
        return result

    def bindings(self):
        with self.connect() as db:
            return [{**dict(row), "sources": json.loads(row["sources"])} for row in db.execute("SELECT * FROM bindings ORDER BY name")]

    def ingest(self, binding, envelope):
        if not isinstance(envelope, dict) or not {"id", "source", "type", "data"} <= envelope.keys():
            raise IngressError("event requires id, source, type and data")
        if envelope.keys() - {"id", "source", "type", "data", "trace_id", "hops"}:
            raise IngressError("unknown event fields")
        for key in ("id", "source", "type", "trace_id"):
            if key in envelope and (not isinstance(envelope[key], str) or not NAME.fullmatch(envelope[key])):
                raise IngressError(f"invalid {key}")
        hops = envelope.get("hops", 0)
        if type(hops) is not int or not 0 <= hops <= 8:
            raise IngressError("hops must be an integer between 0 and 8")
        try:
            encoded = compact(envelope)
        except (ValueError, TypeError, RecursionError) as exc:
            raise IngressError("event must contain finite JSON values") from exc
        if len(encoded.encode()) > 32768:
            raise IngressError("event exceeds 32 KiB", 413)
        delivery_id = str(uuid.uuid4())
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT * FROM bindings WHERE name=?", (binding,)).fetchone()
            if target is None:
                raise IngressError("unknown binding", 404)
            if envelope["source"] not in json.loads(target["sources"]):
                raise IngressError("source is not allowed for this binding", 403)
            existing = db.execute("SELECT * FROM events WHERE binding=? AND source=? AND event_id=?",
                                  (binding, envelope["source"], envelope["id"])).fetchone()
            if existing:
                if existing["envelope"] != encoded:
                    raise IngressError("event ID already used for different content", 409)
                return {"delivery_id": existing["id"], "duplicate": True}
            if not target["enabled"]:
                raise IngressError("binding is disabled", 409)
            count = db.execute("SELECT count(*) FROM events WHERE binding=? AND state IN ('pending','submitting','uncertain')", (binding,)).fetchone()[0]
            if count >= self.max_pending:
                raise IngressError("pending queue capacity reached", 429)
            rate = db.execute("SELECT count(*) FROM events WHERE binding=? AND created>?", (binding, now - 60)).fetchone()[0]
            if rate >= self.rate_limit:
                raise IngressError("binding event rate limit reached", 429)
            if envelope.get("trace_id"):
                count = db.execute("SELECT count(*) FROM events WHERE json_extract(envelope,'$.trace_id')=?", (envelope["trace_id"],)).fetchone()[0]
                if count >= self.trace_limit:
                    raise IngressError("trace conversation budget reached", 429)
            state = "ignored" if envelope["type"] == "agent.ack" else "pending"
            db.execute("INSERT INTO events(id,binding,source,event_id,envelope,client_id,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                       (delivery_id, binding, envelope["source"], envelope["id"], encoded, "codex-monitor:" + delivery_id, state, now, now))
        self.wakeup.set()
        return {"delivery_id": delivery_id, "duplicate": False}

    def event(self, delivery_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        result = dict(row)
        result["envelope"] = json.loads(result["envelope"])
        return result

    def enable(self, binding, enabled):
        with self.connect() as db:
            if not db.execute("UPDATE bindings SET enabled=? WHERE name=?", (int(enabled), binding)).rowcount:
                raise IngressError("unknown binding", 404)
        self.wakeup.set()

    def resolve(self, delivery_id, action, reason):
        if action not in ("accept", "replay", "discard") or not reason.strip():
            raise IngressError("resolution requires accept/replay/discard and a reason")
        state = {"accept": "accepted", "replay": "pending", "discard": "discarded"}[action]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM events WHERE id=?", (delivery_id,)).fetchone()
            if row is None or row["state"] not in ("uncertain", "dead"):
                raise IngressError("only uncertain/dead deliveries can be resolved", 409)
            db.execute("UPDATE events SET state=?,error=NULL,attempts=0,next_at=0,created=?,updated=? WHERE id=?",
                       (state, self.clock(), self.clock(), delivery_id))
            db.execute("INSERT INTO decisions(delivery_id,action,reason,created) VALUES(?,?,?,?)",
                       (delivery_id, action, reason, self.clock()))
        self.wakeup.set()

    def status(self):
        with self.connect() as db:
            counts = {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM events GROUP BY state")}
        return {"bindings": self.bindings(), "events": counts}

    def dispatch_once(self):
        with self.dispatch_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT e.*,b.thread,b.endpoint FROM events e JOIN bindings b ON b.name=e.binding
                                WHERE e.state IN ('pending','submitting','uncertain') AND b.enabled=1 AND e.next_at<=?
                                AND NOT EXISTS(SELECT 1 FROM events prior WHERE prior.binding=e.binding AND prior.seq<e.seq
                                               AND prior.state IN ('pending','submitting','uncertain'))
                                ORDER BY e.next_at,e.seq LIMIT 1""", (self.clock(),)).fetchone()
            if row is None:
                return False
            reconciling = row["state"] != "pending"
            if not reconciling and self.clock() - row["created"] > self.max_age:
                db.execute("UPDATE events SET state='dead',error='event expired',updated=? WHERE id=?", (self.clock(), row["id"]))
                return True
            db.execute("UPDATE events SET state='submitting',attempts=attempts+?,updated=? WHERE id=?",
                       (0 if reconciling else 1, self.clock(), row["id"]))
            db.commit()
            text = render_event(json.loads(row["envelope"]), row["id"], row["binding"])
            try:
                session = self.factory(row["endpoint"])
                if reconciling:
                    receipt = session.reconcile(row["thread"], row["client_id"])
                    if not receipt:
                        raise Uncertain("not found in queue/history; inspect conversation before replay")
                else:
                    receipt = session.deliver(row["thread"], row["client_id"], text)
                db.execute("UPDATE events SET state='accepted',submission_id=?,error=NULL,updated=? WHERE id=?",
                           (receipt["submission_id"], self.clock(), row["id"]))
            except Exception as exc:
                if reconciling or isinstance(exc, Uncertain):
                    state, delay = "uncertain", 30
                elif isinstance(exc, Retryable):
                    state = "dead" if row["attempts"] + 1 >= self.max_attempts else "pending"
                    delay = min(60, 2 ** row["attempts"])
                elif isinstance(exc, Permanent):
                    state, delay = "dead", 0
                else:
                    # An unexpected exception after handing off may hide acceptance.
                    state, delay = "uncertain", 30
                db.execute("UPDATE events SET state=?,error=?,next_at=?,updated=? WHERE id=?",
                           (state, str(exc)[:500], self.clock() + delay, self.clock(), row["id"]))
            return True
