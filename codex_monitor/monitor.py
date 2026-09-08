"""Durable event inbox. Codex owns conversation scheduling and user approvals."""
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import re
from contextlib import contextmanager

from .errors import IngressError, Retryable, Uncertain, Permanent
from .presentation import render_event

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$")


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


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
            """)
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
        if not all(isinstance(x, str) and NAME.fullmatch(x) for x in [name, thread, *sources]) or not sources or "/" in name:
            raise IngressError("binding, thread and sources must be nonempty identifiers")
        with self.connect() as db:
            db.execute("INSERT INTO bindings(name,thread,endpoint,sources) VALUES(?,?,?,?)",
                       (name, thread, endpoint, compact(sources)))
        return {"name": name, "thread": thread, "endpoint": endpoint, "sources": sources}

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
