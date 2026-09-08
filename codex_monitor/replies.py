"""Durable explicit replies, pulled and acknowledged by the original source."""
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .errors import IngressError
from .monitor import NAME


class ReplyStore:
    def __init__(self, root, max_pending=1000):
        self.path = Path(root) / "replies.sqlite3"
        self.max_pending = max_pending
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                delivery_id TEXT NOT NULL, reply_key TEXT NOT NULL, source TEXT NOT NULL,
                binding TEXT NOT NULL, event_id TEXT NOT NULL, message TEXT NOT NULL,
                created REAL NOT NULL, acknowledged REAL,
                UNIQUE(delivery_id,reply_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS replies_pending ON replies(source,acknowledged,seq)")
        os.chmod(self.path, 0o600)

    def connect(self):
        # The connection context below closes explicitly; SQLite's context only commits.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def add(self, parent, reply_key, message):
        if not isinstance(reply_key, str) or not NAME.fullmatch(reply_key):
            raise IngressError("reply id must be a nonempty identifier")
        if not isinstance(message, str) or not message.strip() or len(message.encode()) > 16384:
            raise IngressError("reply message must be nonempty and at most 16 KiB")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM replies WHERE delivery_id=? AND reply_key=?",
                             (parent["id"], reply_key)).fetchone()
            if row:
                if row["message"] != message:
                    raise IngressError("reply id already used for different content", 409)
                return {"reply_id": row["id"], "duplicate": True}
            if db.execute("SELECT count(*) FROM replies WHERE acknowledged IS NULL").fetchone()[0] >= self.max_pending:
                raise IngressError("reply outbox capacity reached", 429)
            reply_id = str(uuid.uuid4())
            db.execute("""INSERT INTO replies(id,delivery_id,reply_key,source,binding,event_id,message,created)
                          VALUES(?,?,?,?,?,?,?,?)""", (reply_id, parent["id"], reply_key, parent["source"],
                          parent["binding"], parent["event_id"], message, time.time()))
        return {"reply_id": reply_id, "duplicate": False}

    def pending(self, source, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise IngressError("reply limit must be 1..100")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM replies WHERE source=? AND acknowledged IS NULL ORDER BY seq LIMIT ?",
                              (source, limit)).fetchall()
        return {"data": [dict(row) for row in rows], "note": "Acknowledge processed reply IDs to read the next batch."}

    def ack(self, source, reply_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT acknowledged FROM replies WHERE id=? AND source=?", (reply_id, source)).fetchone()
            if row is None:
                raise IngressError("reply not found", 404)
            db.execute("UPDATE replies SET acknowledged=COALESCE(acknowledged,?) WHERE id=? AND source=?",
                       (time.time(), reply_id, source))
        return {"reply_id": reply_id, "acknowledged": True}
