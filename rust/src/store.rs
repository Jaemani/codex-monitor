//! Durable Rust runtime store.
//!
//! The Rust runtime deliberately owns a separate database.  It does not open
//! or migrate the Python monitor database; callers should pass a path such as
//! `<state>/rust.sqlite3`.

use anyhow::{Context, Result, anyhow, bail};
use rusqlite::{Connection, OptionalExtension, Row, Transaction, params};
use serde_json::{Map, Value, json};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};
use url::Url;
use uuid::Uuid;

const MAX_IDENTIFIER_BYTES: usize = 200;
const MAX_ENDPOINT_BYTES: usize = 4 * 1024;
const MAX_ENVELOPE_BYTES: usize = 32 * 1024;
const MAX_REPLY_BYTES: usize = 16 * 1024;
const MAX_REQUEST_BYTES: usize = 16 * 1024;
const MAX_DETAIL_BYTES: usize = 4 * 1024;
const MAX_PENDING: i64 = 1_000;
const RATE_LIMIT: i64 = 120;
const TRACE_LIMIT: i64 = 16;
const MAX_AGE_SECONDS: f64 = 3600.0;
const MAX_ATTEMPTS: i64 = 5;
const RETRY_BACKOFF_BASE_SECONDS: f64 = 2.0;
const RETRY_BACKOFF_MAX_SECONDS: f64 = 60.0;
const MAX_WATCHES: i64 = 128;
const MAX_WATCHES_PER_THREAD: i64 = 32;
const MAX_REPLIES: i64 = 1_000;
const MAX_REQUESTS: i64 = 10_000;

const NAME_CHARS: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:@/-";

/// A clone of a store shares one SQLite connection and its transaction lock.
#[derive(Clone)]
pub struct Store {
    path: Arc<PathBuf>,
    conn: Arc<Mutex<Connection>>,
}

impl Store {
    /// Open the Rust-owned database, creating its parent directory and schema.
    pub fn open(path: &Path) -> Result<Self> {
        if path.as_os_str().is_empty() {
            bail!("store path must not be empty");
        }
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create store directory {}", parent.display()))?;
        }
        let existed = path.is_file();
        let conn = Connection::open(path)
            .with_context(|| format!("open Rust store {}", path.display()))?;
        conn.busy_timeout(std::time::Duration::from_secs(10))?;
        if existed {
            let marker_table: Option<String> = conn
                .query_row(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='store_meta'",
                    [],
                    |row| row.get(0),
                )
                .optional()?;
            let existing_schema: Option<String> = conn
                .query_row(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1",
                    [],
                    |row| row.get(0),
                )
                .optional()?;
            if marker_table.is_none() && existing_schema.is_some() {
                bail!("existing database is not a Rust monitor store");
            }
        }
        // WAL and FULL are set once on this long-lived connection.  No watch
        // heartbeat opens another connection or changes the journal mode.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        let store = Self {
            path: Arc::new(path.to_path_buf()),
            conn: Arc::new(Mutex::new(conn)),
        };
        // A marker makes accidental use of an unrelated SQLite file visible,
        // while still allowing a new Rust database to be opened repeatedly.
        store.with_conn(|db| {
            db.execute(
                "INSERT OR IGNORE INTO store_meta(key,value) VALUES('runtime','rust')",
                [],
            )?;
            let runtime: String = db.query_row(
                "SELECT value FROM store_meta WHERE key='runtime'",
                [],
                |row| row.get(0),
            )?;
            if runtime != "rust" {
                bail!("database is not a Rust monitor store");
            }
            Ok(())
        })?;
        Ok(store)
    }

    /// The database path is useful to the runtime for diagnostics.
    pub fn path(&self) -> &Path {
        self.path.as_path()
    }

    fn with_conn<T>(&self, f: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
        let guard = self.lock_conn()?;
        f(&guard)
    }

    fn lock_conn(&self) -> Result<MutexGuard<'_, Connection>> {
        self.conn
            .lock()
            .map_err(|_| anyhow!("store connection mutex poisoned"))
    }

    fn now() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_secs_f64())
            .unwrap_or(0.0)
    }

    /// Register a native route.  Source authorization is kept with the route,
    /// so intake can check the exact route atomically with deduplication.
    pub fn bind(
        &self,
        name: &str,
        thread: &str,
        endpoint: &str,
        sources: &[String],
    ) -> Result<Value> {
        validate_identifier(name, "binding")?;
        if name.contains('/') {
            bail!("binding name must not contain '/'");
        }
        validate_identifier(thread, "thread")?;
        validate_endpoint(endpoint)?;
        if sources.is_empty() {
            bail!("binding sources must not be empty");
        }
        for source in sources {
            validate_identifier(source, "source")?;
            if source == "managed/file" {
                bail!("reserved managed source cannot be registered externally");
            }
        }
        let encoded = canonical_json(&Value::Array(
            sources.iter().cloned().map(Value::String).collect(),
        ))?;
        self.with_tx(|tx| {
            tx.execute(
                "INSERT INTO bindings(name,thread,endpoint,sources,enabled,removed) VALUES(?,?,?, ?,1,0)",
                params![name, thread, endpoint, encoded],
            )?;
            Ok(json!({"name":name,"thread":thread,"endpoint":endpoint,"sources":sources}))
        })
    }

    pub fn bindings(&self) -> Result<Value> {
        self.with_conn(|db| {
            let mut stmt = db.prepare(
                "SELECT name,thread,endpoint,sources,enabled,removed FROM bindings ORDER BY name",
            )?;
            let mut rows = stmt.query([])?;
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                out.push(binding_value(row)?);
            }
            Ok(Value::Array(out))
        })
    }

    pub fn metadata_set(&self, thread: &str, project: &str, display_name: &str) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        let project = validate_text(project, "project", 200)?;
        let display_name = validate_text(display_name, "display name", 200)?;
        self.with_tx(|tx| {
            tx.execute(
                "INSERT INTO conversation_metadata(thread,project,display_name) VALUES(?,?,?) \
                 ON CONFLICT(thread) DO UPDATE SET project=excluded.project,display_name=excluded.display_name",
                params![thread, project, display_name],
            )?;
            Ok(json!({"thread":thread,"project":project,"display_name":display_name}))
        })
    }

    pub fn metadata_list(&self, thread: Option<&str>) -> Result<Value> {
        if let Some(thread) = thread {
            validate_identifier(thread, "thread")?;
        }
        self.with_conn(|db| {
            let mut out = Map::new();
            if let Some(thread) = thread {
                let row = db
                    .query_row(
                        "SELECT thread,project,display_name FROM conversation_metadata WHERE thread=?",
                        [thread],
                        |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?)),
                    )
                    .optional()?;
                if let Some((t, project, name)) = row {
                    out.insert(t.clone(), json!({"thread":t,"project":project,"display_name":name}));
                }
                let exists: Option<String> = db
                    .query_row("SELECT thread FROM bindings WHERE thread=? LIMIT 1", [thread], |r| r.get(0))
                    .optional()?;
                if out.is_empty() && exists.is_some() {
                    out.insert(thread.to_owned(), json!({"thread":thread,"project":"Ungrouped","display_name":thread}));
                }
            } else {
                let mut stmt = db.prepare("SELECT thread,project,display_name FROM conversation_metadata")?;
                let mut rows = stmt.query([])?;
                while let Some(row) = rows.next()? {
                    let t: String = row.get(0)?;
                    out.insert(t.clone(), json!({"thread":t,"project":row.get::<_,String>(1)?,"display_name":row.get::<_,String>(2)?}));
                }
                let mut stmt = db.prepare("SELECT DISTINCT thread FROM bindings")?;
                let mut rows = stmt.query([])?;
                while let Some(row) = rows.next()? {
                    let t: String = row.get(0)?;
                    out.entry(t.clone()).or_insert_with(|| json!({"thread":t,"project":"Ungrouped","display_name":t}));
                }
            }
            let mut values: Vec<Value> = out.into_values().collect();
            values.sort_by(|a, b| a["thread"].as_str().cmp(&b["thread"].as_str()));
            Ok(Value::Array(values))
        })
    }

    pub fn route_action(&self, name: &str, action: &str) -> Result<Value> {
        validate_identifier(name, "binding")?;
        if !matches!(action, "pause" | "resume" | "remove") {
            bail!("route action must be pause, resume, or remove");
        }
        self.with_tx(|tx| {
            let row = tx
                .query_row(
                    "SELECT thread,endpoint,removed FROM bindings WHERE name=?",
                    [name],
                    |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?)),
                )
                .optional()?;
            let Some((thread, endpoint, removed)) = row else {
                bail!("unknown binding");
            };
            if removed != 0 && action != "remove" {
                bail!("binding has been retired");
            }
            let enabled = action == "resume";
            let retired = action == "remove";
            tx.execute(
                "UPDATE bindings SET enabled=?,removed=? WHERE name=?",
                params![enabled as i64, retired as i64, name],
            )?;
            tx.execute(
                "UPDATE managed_watches SET enabled=?,removed=?,epoch=epoch+1 WHERE binding=? AND removed=0",
                params![enabled as i64, retired as i64, name],
            )?;
            if retired {
                // A removed binding can no longer dispatch work that has not
                // crossed the native handoff boundary. Keep those receipts
                // visible, but settle them instead of leaving them pending
                // forever behind a route that can never be resumed.
                tx.execute(
                    "UPDATE events SET state='dead',error='binding removed',next_at=0,updated=? \
                     WHERE binding=? AND state='pending'",
                    params![Self::now(), name],
                )?;
            }
            Ok(json!({"action":action,"binding":name,"thread":thread,"endpoint":endpoint,"enabled":enabled,"removed":retired}))
        })
    }

    pub fn ingest(&self, binding: &str, envelope: &Value) -> Result<Value> {
        validate_identifier(binding, "binding")?;
        self.with_tx(|tx| self.ingest_tx(tx, binding, envelope))
    }

    fn ingest_tx(&self, tx: &Transaction<'_>, binding: &str, envelope: &Value) -> Result<Value> {
        let (source, event_id, encoded) = validate_envelope(envelope)?;
        let target = tx
            .query_row(
                "SELECT thread,endpoint,sources,enabled,removed FROM bindings WHERE name=?",
                [binding],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, i64>(3)?,
                        r.get::<_, i64>(4)?,
                    ))
                },
            )
            .optional()?;
        let Some((_thread, _endpoint, sources_json, enabled, removed)) = target else {
            bail!("unknown binding");
        };
        let sources: Value =
            serde_json::from_str(&sources_json).context("decode binding sources")?;
        if !sources
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .any(|item| item.as_str() == Some(source.as_str()))
            })
            .unwrap_or(false)
        {
            bail!("source is not allowed for this binding");
        }
        // Idempotent intake remains readable after a route is paused.  A
        // different payload for the same binding/source/event id is a conflict.
        let existing = tx
            .query_row(
                "SELECT id,envelope,state FROM events WHERE binding=? AND source=? AND event_id=?",
                params![binding, source, event_id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()?;
        if let Some((id, prior, state)) = existing {
            if prior != encoded {
                bail!("event ID already used for different content");
            }
            return Ok(json!({"delivery_id":id,"state":state,"duplicate":true}));
        }
        if enabled == 0 || removed != 0 {
            bail!("binding is disabled");
        }
        let now = Self::now();
        let pending: i64 = tx.query_row(
            "SELECT count(*) FROM events WHERE binding=? AND state IN ('pending','submitting','uncertain')",
            [binding], |r| r.get(0))?;
        if pending >= MAX_PENDING {
            bail!("pending queue capacity reached");
        }
        let recent: i64 = tx.query_row(
            "SELECT count(*) FROM events WHERE binding=? AND created>?",
            params![binding, now - 60.0],
            |r| r.get(0),
        )?;
        if recent >= RATE_LIMIT {
            bail!("binding event rate limit reached");
        }
        let trace_id = envelope.get("trace_id").and_then(Value::as_str);
        if let Some(trace_id) = trace_id {
            let trace_count: i64 = tx.query_row(
                "SELECT count(*) FROM events WHERE json_extract(envelope,'$.trace_id')=?",
                [trace_id],
                |r| r.get(0),
            )?;
            if trace_count >= TRACE_LIMIT {
                bail!("trace conversation budget reached");
            }
        }
        let id = Uuid::new_v4().to_string();
        let client_id = format!("codex-monitor:{id}");
        let state = if envelope["type"].as_str() == Some("agent.ack") {
            "ignored"
        } else {
            "pending"
        };
        tx.execute(
            "INSERT INTO events(id,binding,source,event_id,envelope,client_id,state,created,updated,attempts,next_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            params![id, binding, source, event_id, encoded, client_id, state, now, now, 0_i64, 0.0_f64],
        )?;
        Ok(json!({"delivery_id":id,"state":state,"duplicate":false}))
    }

    pub fn event(&self, id: &str) -> Result<Value> {
        validate_identifier(id, "delivery id")?;
        self.with_conn(|db| {
            let row = db
                .query_row("SELECT * FROM events WHERE id=?", [id], event_row)
                .optional()?;
            row.ok_or_else(|| anyhow!("event not found"))
        })
    }

    pub fn status(&self) -> Result<Value> {
        self.with_conn(|db| {
            let bindings: i64 = db.query_row("SELECT count(*) FROM bindings", [], |r| r.get(0))?;
            let watches: i64 = db.query_row("SELECT count(*) FROM managed_watches WHERE removed=0", [], |r| r.get(0))?;
            let replies: i64 = db.query_row("SELECT count(*) FROM replies WHERE acknowledged IS NULL", [], |r| r.get(0))?;
            let requests: i64 = db.query_row("SELECT count(*) FROM requests", [], |r| r.get(0))?;
            let mut stmt = db.prepare("SELECT state,count(*) FROM events GROUP BY state")?;
            let mut rows = stmt.query([])?;
            let mut counts = Map::new();
            for state in ["pending", "submitting", "uncertain", "accepted", "dead", "ignored"] {
                counts.insert(state.to_owned(), json!(0));
            }
            while let Some(row) = rows.next()? {
                let state: String = row.get(0)?;
                let count: i64 = row.get(1)?;
                counts.insert(state, json!(count));
            }
            Ok(json!({"bindings":bindings,"watches":watches,"replies_pending":replies,"requests":requests,"events":Value::Object(counts)}))
        })
    }

    /// Atomically claim the next event.  A pending claim spends one attempt;
    /// `defer_unavailable` returns a preflight failure without spending that
    /// attempt. Recovered uncertain events do not spend another one.
    pub fn claim(&self) -> Result<Option<Value>> {
        self.with_tx(|tx| {
            loop {
                let now = Self::now();
                let row = tx
                    .query_row(
                        "SELECT e.* FROM events e JOIN bindings b ON b.name=e.binding \
                         WHERE e.state='pending' AND b.enabled=1 AND b.removed=0 AND e.next_at<=? \
                         AND NOT EXISTS (SELECT 1 FROM events prior WHERE prior.binding=e.binding AND prior.seq<e.seq \
                                         AND prior.state IN ('pending','submitting','uncertain')) \
                        ORDER BY e.next_at,e.seq LIMIT 1",
                        [now],
                        event_claim_row,
                    )
                    .optional()?;
                let Some((id, state, created)) = row else { return Ok(None); };
                if state == "pending" && now - created > MAX_AGE_SECONDS {
                    tx.execute("UPDATE events SET state='dead',error='event expired',updated=? WHERE id=?", params![now, id])?;
                    continue;
                }
                if state == "pending" {
                    tx.execute("UPDATE events SET state='submitting',attempts=attempts+1,updated=? WHERE id=? AND state='pending'", params![now, id])?;
                } else {
                    tx.execute("UPDATE events SET state='submitting',updated=? WHERE id=? AND state=?", params![now, id, state])?;
                }
                let result = tx.query_row(
                    "SELECT e.*,b.thread,b.endpoint FROM events e JOIN bindings b ON b.name=e.binding WHERE e.id=?",
                    [id],
                    event_claim_value,
                )?;
                return Ok(Some(result));
            }
        })
    }

    pub fn finish(
        &self,
        id: &str,
        state: &str,
        submission_id: Option<&str>,
        error: Option<&str>,
    ) -> Result<()> {
        validate_identifier(id, "delivery id")?;
        if !matches!(
            state,
            "pending" | "accepted" | "uncertain" | "dead" | "ignored"
        ) {
            bail!("invalid event state");
        }
        if let Some(value) = submission_id {
            validate_identifier(value, "submission id")?;
        }
        if let Some(value) = error
            && value.len() > 4096
        {
            bail!("event error is too long");
        }
        self.with_tx(|tx| {
            let prior: Option<(i64, String, Option<String>)> = tx
                .query_row("SELECT attempts,state,submission_id FROM events WHERE id=?", [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .optional()?;
            let Some((attempts, prior_state, prior_submission)) = prior else { bail!("event not found"); };
            if prior_state == "accepted" {
                if state == "accepted" && prior_submission.as_deref() == submission_id {
                    return Ok(());
                }
                bail!("accepted event cannot be finished with different content");
            }
            if !matches!(prior_state.as_str(), "submitting" | "uncertain") {
                bail!("event is not in flight");
            }
            let (stored_state, stored_error) = if state == "pending" && attempts >= MAX_ATTEMPTS {
                ("dead", Some(error.unwrap_or("delivery retry limit reached")))
            } else {
                (state, error)
            };
            let now = Self::now();
            let next_at = if stored_state == "pending" {
                now + retry_delay(attempts)
            } else {
                0.0
            };
            tx.execute(
                "UPDATE events SET state=?,submission_id=?,error=?,attempts=?,next_at=?,updated=? WHERE id=?",
                params![stored_state, submission_id, stored_error, attempts, next_at, now, id],
            )?;
            Ok(())
        })
    }

    /// Return an event to pending after endpoint preflight failed.  This is
    /// intentionally separate from a transport failure: no delivery attempt
    /// is consumed when the runtime never reached the native endpoint.
    pub fn defer_unavailable(&self, id: &str, error: &str) -> Result<()> {
        validate_identifier(id, "delivery id")?;
        if error.len() > 4096 {
            bail!("event error is too long");
        }
        self.with_tx(|tx| {
            let prior: Option<(i64, String)> = tx
                .query_row("SELECT attempts,state FROM events WHERE id=?", [id], |r| {
                    Ok((r.get(0)?, r.get(1)?))
                })
                .optional()?;
            let Some((attempts, prior_state)) = prior else {
                bail!("event not found");
            };
            if prior_state != "submitting" {
                bail!("event is not in flight");
            }
            let now = Self::now();
            tx.execute(
                "UPDATE events SET state='pending',attempts=?,next_at=?,error=?,updated=? WHERE id=?",
                params![attempts.saturating_sub(1), now + 5.0, error, now, id],
            )?;
            Ok(())
        })
    }

    /// Read uncertain handoffs without claiming them again.  A dispatcher can
    /// reconcile these stable client IDs with the native queue/history before
    /// deciding whether to finish them as accepted or dead.
    pub fn uncertain_events(&self) -> Result<Value> {
        self.with_conn(|db| {
            let mut stmt = db.prepare(
                "SELECT e.*,b.thread,b.endpoint FROM events e JOIN bindings b ON b.name=e.binding \
                 WHERE e.state='uncertain' ORDER BY e.seq",
            )?;
            let mut rows = stmt.query([])?;
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                out.push(event_claim_value(row)?);
            }
            Ok(Value::Array(out))
        })
    }

    /// Startup recovery makes handoffs explicit.  The dispatcher must inspect
    /// uncertain receipts before choosing whether to retry them.
    pub fn recover_submitting(&self) -> Result<()> {
        self.with_tx(|tx| {
            tx.execute(
                "UPDATE events SET state='uncertain',next_at=?,updated=? WHERE state='submitting'",
                params![Self::now(), Self::now()],
            )?;
            Ok(())
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn watch_create(
        &self,
        thread: &str,
        name: &str,
        path: &str,
        interval: f64,
        endpoint: &str,
        debounce: f64,
        condition: Option<Value>,
    ) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        validate_identifier(name, "watch name")?;
        if name.contains('/') {
            bail!("watch name must not contain '/'");
        }
        if !Path::new(path).is_absolute() {
            bail!("managed file path must be absolute");
        }
        if !interval.is_finite() || !(0.1..=86400.0).contains(&interval) {
            bail!("managed watch interval must be between 0.1 and 86400 seconds");
        }
        if !debounce.is_finite() || !(0.0..=86400.0).contains(&debounce) {
            bail!("debounce must be between 0 and 86400 seconds");
        }
        validate_endpoint(endpoint)?;
        let condition_json = match &condition {
            Some(value) => Some(canonical_json(value)?),
            None => None,
        };
        let id = Uuid::new_v4().to_string();
        let binding = format!("managed-{id}");
        let now = Self::now();
        self.with_tx(|tx| {
            let duplicate: i64 = tx.query_row(
                "SELECT count(*) FROM managed_watches WHERE thread=? AND name=? AND removed=0",
                params![thread, name], |r| r.get(0))?;
            if duplicate > 0 { bail!("a managed watch with this name already exists for the thread"); }
            let total: i64 = tx.query_row("SELECT count(*) FROM managed_watches WHERE removed=0", [], |r| r.get(0))?;
            let per_thread: i64 = tx.query_row("SELECT count(*) FROM managed_watches WHERE thread=? AND removed=0", [thread], |r| r.get(0))?;
            if total >= MAX_WATCHES { bail!("managed watch capacity reached"); }
            if per_thread >= MAX_WATCHES_PER_THREAD { bail!("managed watch capacity for this thread reached"); }
            tx.execute(
                "INSERT INTO bindings(name,thread,endpoint,sources,enabled,removed) VALUES(?,?,?,'[\"managed/file\"]',1,0)",
                params![binding, thread, endpoint],
            )?;
            tx.execute(
                "INSERT INTO managed_watches(id,thread,name,path,interval,endpoint,debounce,condition,binding,enabled,removed,epoch,created,updated) VALUES(?,?,?,?,?,?,?,?,?,1,0,0,?,?)",
                params![id, thread, name, path, interval, endpoint, debounce, condition_json, binding, now, now],
            )?;
            Ok(json!({"id":id,"thread":thread,"name":name,"path":path,"interval":interval,"endpoint":endpoint,"debounce":debounce,"condition":condition,"binding":binding,"enabled":true,"removed":false,"epoch":0}))
        })
    }

    pub fn watches(&self, thread: Option<&str>) -> Result<Value> {
        if let Some(thread) = thread {
            validate_identifier(thread, "thread")?;
        }
        self.with_conn(|db| {
            let sql = if thread.is_some() {
                "SELECT * FROM managed_watches WHERE thread=? AND removed=0 ORDER BY name"
            } else {
                "SELECT * FROM managed_watches WHERE removed=0 ORDER BY thread,name"
            };
            let mut stmt = db.prepare(sql)?;
            let mut rows = if let Some(thread) = thread {
                stmt.query([thread])?
            } else {
                stmt.query([])?
            };
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                out.push(watch_value(row)?);
            }
            Ok(Value::Array(out))
        })
    }

    pub fn watch_action(&self, thread: &str, name: &str, action: &str) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        validate_identifier(name, "watch name")?;
        if !matches!(action, "pause" | "resume" | "remove") {
            bail!("watch action must be pause, resume, or remove");
        }
        self.with_tx(|tx| {
            let id: Option<String> = tx.query_row(
                "SELECT id FROM managed_watches WHERE thread=? AND name=? AND removed=0 ORDER BY created DESC LIMIT 1",
                params![thread, name], |r| r.get(0)).optional()?;
            let Some(id) = id else { bail!("unknown managed watch"); };
            let enabled = action == "resume";
            let removed = action == "remove";
            tx.execute("UPDATE managed_watches SET enabled=?,removed=?,epoch=epoch+1,updated=? WHERE id=?", params![enabled as i64, removed as i64, Self::now(), id])?;
            tx.execute("UPDATE bindings SET enabled=?,removed=? WHERE name=(SELECT binding FROM managed_watches WHERE id=?)", params![enabled as i64, removed as i64, id])?;
            let mut row = tx.query_row("SELECT * FROM managed_watches WHERE id=?", [id], watch_value)?;
            row["action"] = json!(action);
            Ok(row)
        })
    }

    pub fn checkpoint(&self, id: &str) -> Result<Option<Value>> {
        validate_identifier(id, "watch id")?;
        self.with_conn(|db| {
            let value: Option<Option<String>> = db
                .query_row(
                    "SELECT checkpoint FROM managed_watches WHERE id=?",
                    [id],
                    |r| r.get(0),
                )
                .optional()?;
            value
                .flatten()
                .map(|encoded| serde_json::from_str(&encoded).context("decode checkpoint"))
                .transpose()
        })
    }

    pub fn commit_sample(
        &self,
        id: &str,
        epoch: i64,
        checkpoint: &Value,
        event: Option<&Value>,
    ) -> Result<Option<Value>> {
        validate_identifier(id, "watch id")?;
        if epoch < 0 {
            bail!("watch epoch must be nonnegative");
        }
        let checkpoint_json = canonical_json(checkpoint)?;
        if checkpoint_json.len() > MAX_ENVELOPE_BYTES {
            bail!("checkpoint exceeds 32 KiB");
        }
        self.with_tx(|tx| {
            let row = tx
                .query_row(
                    "SELECT binding,enabled,removed,epoch FROM managed_watches WHERE id=?",
                    [id],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, i64>(1)?,
                            r.get::<_, i64>(2)?,
                            r.get::<_, i64>(3)?,
                        ))
                    },
                )
                .optional()?;
            let Some((binding, enabled, removed, current_epoch)) = row else {
                bail!("unknown managed watch");
            };
            if current_epoch != epoch {
                bail!("stale managed watch epoch");
            }
            if enabled == 0 || removed != 0 {
                bail!("managed watch is disabled");
            }
            let receipt = event
                .map(|value| self.ingest_tx(tx, &binding, value))
                .transpose()?;
            tx.execute(
                "UPDATE managed_watches SET checkpoint=?,updated=? WHERE id=? AND epoch=?",
                params![checkpoint_json, Self::now(), id, epoch],
            )?;
            Ok(receipt)
        })
    }

    pub fn reply_put(&self, delivery_id: &str, reply_key: &str, message: &str) -> Result<Value> {
        validate_identifier(delivery_id, "delivery id")?;
        validate_identifier(reply_key, "reply id")?;
        let message = validate_text(message, "reply message", MAX_REPLY_BYTES)?;
        self.with_tx(|tx| {
            let parent = tx.query_row("SELECT source,binding,event_id FROM events WHERE id=?", [delivery_id], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).optional()?;
            let Some((source,binding,event_id)) = parent else { bail!("event not found"); };
            let prior = tx.query_row("SELECT id,message FROM replies WHERE delivery_id=? AND reply_key=?", params![delivery_id,reply_key], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()?;
            if let Some((id, old)) = prior {
                if old != message { bail!("reply id already used for different content"); }
                return Ok(json!({"reply_id":id,"duplicate":true}));
            }
            let pending: i64 = tx.query_row("SELECT count(*) FROM replies WHERE acknowledged IS NULL", [], |r| r.get(0))?;
            if pending >= MAX_REPLIES { bail!("reply outbox capacity reached"); }
            let id = Uuid::new_v4().to_string();
            tx.execute("INSERT INTO replies(id,delivery_id,reply_key,source,binding,event_id,message,created) VALUES(?,?,?,?,?,?,?,?)", params![id,delivery_id,reply_key,source,binding,event_id,message,Self::now()])?;
            Ok(json!({"reply_id":id,"duplicate":false}))
        })
    }

    pub fn replies(&self, source: &str) -> Result<Value> {
        validate_identifier(source, "source")?;
        self.with_conn(|db| {
            let mut stmt = db.prepare("SELECT id,delivery_id,reply_key,source,binding,event_id,message,created,acknowledged FROM replies WHERE source=? AND acknowledged IS NULL ORDER BY seq")?;
            let mut rows = stmt.query([source])?;
            let mut data = Vec::new();
            while let Some(row) = rows.next()? { data.push(json!({"reply_id":row.get::<_,String>(0)?,"delivery_id":row.get::<_,String>(1)?,"reply_key":row.get::<_,String>(2)?,"source":row.get::<_,String>(3)?,"binding":row.get::<_,String>(4)?,"event_id":row.get::<_,String>(5)?,"message":row.get::<_,String>(6)?,"created":row.get::<_,f64>(7)?,"acknowledged":row.get::<_,Option<f64>>(8)?})); }
            Ok(json!({"data":data,"note":"Acknowledge processed reply IDs to read the next batch."}))
        })
    }

    pub fn reply_ack(&self, source: &str, reply_id: &str) -> Result<Value> {
        validate_identifier(source, "source")?;
        validate_identifier(reply_id, "reply id")?;
        self.with_tx(|tx| {
            let exists: Option<Option<f64>> = tx
                .query_row(
                    "SELECT acknowledged FROM replies WHERE id=? AND source=?",
                    params![reply_id, source],
                    |r| r.get(0),
                )
                .optional()?;
            if exists.is_none() {
                bail!("reply not found");
            }
            tx.execute(
                "UPDATE replies SET acknowledged=COALESCE(acknowledged,?) WHERE id=? AND source=?",
                params![Self::now(), reply_id, source],
            )?;
            Ok(json!({"reply_id":reply_id,"acknowledged":true}))
        })
    }

    pub fn request_create(
        &self,
        thread: &str,
        source: &str,
        key: &str,
        delivery_id: &str,
        payload: &Value,
        expires_at: Option<f64>,
    ) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        validate_identifier(source, "source")?;
        validate_identifier(key, "request key")?;
        validate_identifier(delivery_id, "delivery id")?;
        let payload_json = canonical_json(payload)?;
        if payload_json.len() > MAX_REQUEST_BYTES {
            bail!("request payload exceeds 16 KiB");
        }
        if let Some(value) = expires_at
            && (!value.is_finite() || value < 0.0)
        {
            bail!("expiry must be finite and nonnegative");
        }
        self.with_tx(|tx| {
            let parent = tx.query_row("SELECT source,binding FROM events WHERE id=?", [delivery_id], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()?;
            let Some((event_source,binding)) = parent else { bail!("event not found"); };
            if event_source != source { bail!("request source does not match delivery"); }
            let target_thread: String = tx.query_row("SELECT thread FROM bindings WHERE name=?", [binding.as_str()], |r| r.get(0))?;
            if target_thread != thread { bail!("request thread does not match delivery"); }
            let old = tx.query_row("SELECT * FROM requests WHERE thread=? AND source=? AND request_key=?", params![thread,source,key], request_value).optional()?;
            if let Some(value) = old {
                if value["delivery_id"] != delivery_id || canonical_json(&value["payload"])? != payload_json || value["expires_at"].as_f64() != expires_at { bail!("request key already belongs to different immutable content"); }
                let mut result = value; result["duplicate"] = json!(true); return Ok(result);
            }
            let count: i64 = tx.query_row("SELECT count(*) FROM requests", [], |r| r.get(0))?;
            if count >= MAX_REQUESTS { bail!("request capacity reached"); }
            let id = Uuid::new_v4().to_string();
            let now = Self::now();
            tx.execute("INSERT INTO requests(request_id,thread,source,request_key,delivery_id,binding,payload,state,revision,expires_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", params![id,thread,source,key,delivery_id,binding,payload_json,"received",0i64,expires_at,now,now])?;
            let mut value = tx.query_row("SELECT * FROM requests WHERE request_id=?", [id], request_value)?;
            value["duplicate"] = json!(false);
            Ok(value)
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn request_update(
        &self,
        thread: &str,
        source: &str,
        key: &str,
        update_id: &str,
        state: &str,
        expected_revision: Option<i64>,
        detail: Option<&str>,
    ) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        validate_identifier(source, "source")?;
        validate_identifier(key, "request key")?;
        validate_identifier(update_id, "update id")?;
        if !matches!(
            state,
            "acknowledged" | "in_progress" | "completed" | "failed" | "cancelled" | "expired"
        ) {
            bail!("unknown request state");
        }
        if let Some(revision) = expected_revision
            && revision < 0
        {
            bail!("expected revision must be nonnegative");
        }
        if let Some(detail) = detail {
            validate_text(detail, "request detail", MAX_DETAIL_BYTES)?;
        }
        self.with_tx(|tx| {
            let row = tx.query_row("SELECT * FROM requests WHERE thread=? AND source=? AND request_key=?", params![thread,source,key], request_value).optional()?;
            let Some(current) = row else { bail!("request not found"); };
            let current_revision = current["revision"].as_i64().unwrap_or(0);
            let expected = expected_revision.unwrap_or(current_revision);
            let prior = tx.query_row("SELECT request_id,state,revision,detail FROM request_updates WHERE request_id=? AND update_id=?", params![current["request_id"].as_str().unwrap_or(""),update_id], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,Option<String>>(3)?))).optional()?;
            if let Some((_, prior_state, prior_revision, prior_detail)) = prior {
                if prior_state != state || prior_revision != expected || prior_detail.as_deref() != detail { bail!("update id already used for different content"); }
                let mut value = tx.query_row("SELECT * FROM requests WHERE request_id=?", [current["request_id"].as_str().unwrap_or("")], request_value)?;
                value["duplicate"] = json!(true);
                value["update_id"] = json!(update_id);
                return Ok(value);
            }
            if expected != current_revision { bail!("request revision conflict"); }
            let previous = current["state"].as_str().unwrap_or("");
            if !allowed_transition(previous, state) { bail!("request state transition is not allowed"); }
            let new_revision = expected + 1;
            tx.execute("INSERT INTO request_updates(request_id,update_id,expected_revision,revision,previous_state,state,detail,created) VALUES(?,?,?,?,?,?,?,?)", params![current["request_id"].as_str().unwrap_or(""),update_id,expected,new_revision,previous,state,detail,Self::now()])?;
            tx.execute("UPDATE requests SET state=?,revision=?,updated=? WHERE request_id=? AND revision=?", params![state,new_revision,Self::now(),current["request_id"].as_str().unwrap_or(""),expected])?;
            let mut value = tx.query_row("SELECT * FROM requests WHERE request_id=?", [current["request_id"].as_str().unwrap_or("")], request_value)?;
            value["duplicate"] = json!(false);
            value["update_id"] = json!(update_id);
            Ok(value)
        })
    }

    pub fn requests(&self, thread: &str, source: Option<&str>) -> Result<Value> {
        validate_identifier(thread, "thread")?;
        if let Some(source) = source {
            validate_identifier(source, "source")?;
        }
        self.with_conn(|db| {
            let mut out = Vec::new();
            let mut stmt = if source.is_some() {
                db.prepare("SELECT * FROM requests WHERE thread=? AND source=? ORDER BY seq")?
            } else {
                db.prepare("SELECT * FROM requests WHERE thread=? ORDER BY seq")?
            };
            let mut rows = if let Some(source) = source {
                stmt.query(params![thread, source])?
            } else {
                stmt.query([thread])?
            };
            while let Some(row) = rows.next()? {
                out.push(request_value(row)?);
            }
            Ok(Value::Array(out))
        })
    }

    fn with_tx<T>(&self, f: impl FnOnce(&Transaction<'_>) -> Result<T>) -> Result<T> {
        let mut guard = self.lock_conn()?;
        let tx = guard.transaction()?;
        let value = f(&tx)?;
        tx.commit()?;
        Ok(value)
    }
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings(
 name TEXT PRIMARY KEY, thread TEXT NOT NULL, endpoint TEXT NOT NULL, sources TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_metadata(
 thread TEXT PRIMARY KEY, project TEXT NOT NULL, display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, binding TEXT NOT NULL,
 source TEXT NOT NULL, event_id TEXT NOT NULL, envelope TEXT NOT NULL, client_id TEXT UNIQUE NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, submission_id TEXT, error TEXT,
 UNIQUE(binding,source,event_id)
);
CREATE INDEX IF NOT EXISTS event_binding_order ON events(binding,seq);
CREATE INDEX IF NOT EXISTS event_dispatch ON events(state,next_at,seq);
CREATE TABLE IF NOT EXISTS managed_watches(
 id TEXT PRIMARY KEY, thread TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
 interval REAL NOT NULL, endpoint TEXT NOT NULL, debounce REAL NOT NULL DEFAULT 0,
 condition TEXT, binding TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 removed INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0, checkpoint TEXT,
 created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS managed_active ON managed_watches(thread,name,removed);
CREATE TABLE IF NOT EXISTS replies(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, delivery_id TEXT NOT NULL,
 reply_key TEXT NOT NULL, source TEXT NOT NULL, binding TEXT NOT NULL, event_id TEXT NOT NULL,
 message TEXT NOT NULL, created REAL NOT NULL, acknowledged REAL,
 UNIQUE(delivery_id,reply_key)
);
CREATE INDEX IF NOT EXISTS replies_pending ON replies(source,acknowledged,seq);
CREATE TABLE IF NOT EXISTS requests(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, thread TEXT NOT NULL,
 source TEXT NOT NULL, request_key TEXT NOT NULL, delivery_id TEXT NOT NULL, binding TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 expires_at REAL, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(thread,source,request_key)
);
CREATE TABLE IF NOT EXISTS request_updates(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, update_id TEXT NOT NULL,
 expected_revision INTEGER NOT NULL, revision INTEGER NOT NULL, previous_state TEXT NOT NULL,
 state TEXT NOT NULL, detail TEXT, created REAL NOT NULL, UNIQUE(request_id,update_id)
);
"#;

fn validate_identifier(value: &str, label: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > MAX_IDENTIFIER_BYTES
        || !value.bytes().enumerate().all(|(i, byte)| {
            NAME_CHARS.as_bytes().contains(&byte) && (i != 0 || byte.is_ascii_alphanumeric())
        })
    {
        bail!("{label} must be a nonempty identifier");
    }
    Ok(())
}

fn retry_delay(attempts: i64) -> f64 {
    let exponent = attempts.saturating_sub(1).clamp(0, 6) as i32;
    (RETRY_BACKOFF_BASE_SECONDS * 2f64.powi(exponent)).min(RETRY_BACKOFF_MAX_SECONDS)
}

fn validate_text(value: &str, label: &str, max_bytes: usize) -> Result<String> {
    if value.trim().is_empty() || value.len() > max_bytes || value.chars().any(|c| c.is_control()) {
        bail!("{label} is empty, too long, or contains control characters");
    }
    Ok(value.to_owned())
}

fn validate_endpoint(endpoint: &str) -> Result<()> {
    if endpoint.is_empty()
        || endpoint.len() > MAX_ENDPOINT_BYTES
        || endpoint.chars().any(char::is_whitespace)
        || endpoint.contains('\0')
    {
        bail!("endpoint is malformed");
    }
    if matches!(endpoint, "shared-local" | "local" | "unix://") {
        return Ok(());
    }
    if let Some(path) = endpoint.strip_prefix("unix://") {
        if !path.starts_with('/') {
            bail!("Unix endpoint must use an absolute socket path");
        }
        return Ok(());
    }
    if let Some(rest) = endpoint.strip_prefix("ssh://") {
        if rest.is_empty()
            || rest.len() > 100
            || !rest.bytes().enumerate().all(|(index, byte)| {
                if index == 0 {
                    byte.is_ascii_alphanumeric()
                } else {
                    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.' || byte == b'-'
                }
            })
        {
            bail!("SSH endpoint alias is malformed");
        }
        return Ok(());
    }
    if endpoint.starts_with("ws://") || endpoint.starts_with("wss://") {
        let url = Url::parse(endpoint).map_err(|_| anyhow!("endpoint URL is malformed"))?;
        if !matches!(url.scheme(), "ws" | "wss")
            || url.host_str().is_none()
            || url.username() != ""
            || url.password().is_some()
            || url.fragment().is_some()
        {
            bail!("endpoint URL is malformed or contains credentials");
        }
        if url.scheme() == "ws"
            && !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
        {
            bail!("ws endpoint must use a loopback host");
        }
        return Ok(());
    }
    bail!(
        "endpoint must be shared-local, local, unix:///absolute/path, ssh://ALIAS, ws://loopback, or wss://"
    )
}

fn canonical_json(value: &Value) -> Result<String> {
    fn write(value: &Value, out: &mut String) -> Result<()> {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
            Value::Number(value) => {
                if !value.is_f64() && !value.is_i64() && !value.is_u64() {
                    bail!("JSON number is invalid");
                }
                out.push_str(&value.to_string());
            }
            Value::String(value) => out.push_str(&serde_json::to_string(value)?),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index != 0 {
                        out.push(',');
                    }
                    write(item, out)?;
                }
                out.push(']');
            }
            Value::Object(items) => {
                out.push('{');
                let mut keys: Vec<&String> = items.keys().collect();
                keys.sort();
                for (index, key) in keys.into_iter().enumerate() {
                    if index != 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key)?);
                    out.push(':');
                    write(&items[key], out)?;
                }
                out.push('}');
            }
        }
        Ok(())
    }
    let mut out = String::new();
    write(value, &mut out)?;
    Ok(out)
}

fn validate_envelope(envelope: &Value) -> Result<(String, String, String)> {
    let Value::Object(map) = envelope else {
        bail!("event must be an object");
    };
    for key in map.keys() {
        if !matches!(
            key.as_str(),
            "id" | "source" | "type" | "data" | "trace_id" | "hops"
        ) {
            bail!("unknown event field {key}");
        }
    }
    for key in ["id", "source", "type", "data"] {
        if !map.contains_key(key) {
            bail!("event requires {key}");
        }
    }
    let id = map["id"]
        .as_str()
        .ok_or_else(|| anyhow!("event id must be an identifier"))?;
    let source = map["source"]
        .as_str()
        .ok_or_else(|| anyhow!("event source must be an identifier"))?;
    let event_type = map["type"]
        .as_str()
        .ok_or_else(|| anyhow!("event type must be an identifier"))?;
    validate_identifier(id, "event id")?;
    validate_identifier(source, "event source")?;
    validate_identifier(event_type, "event type")?;
    if let Some(trace_id) = map.get("trace_id") {
        validate_identifier(
            trace_id
                .as_str()
                .ok_or_else(|| anyhow!("trace_id must be an identifier"))?,
            "trace_id",
        )?;
    }
    let hops = match map.get("hops") {
        None => 0,
        Some(value) => value
            .as_i64()
            .ok_or_else(|| anyhow!("hops must be an integer between 0 and 8"))?,
    };
    if !(0..=8).contains(&hops) {
        bail!("hops must be an integer between 0 and 8");
    }
    let encoded = canonical_json(envelope)?;
    if encoded.len() > MAX_ENVELOPE_BYTES {
        bail!("event exceeds 32 KiB");
    }
    Ok((source.to_owned(), id.to_owned(), encoded))
}

fn binding_value(row: &Row<'_>) -> Result<Value> {
    let sources: Value = serde_json::from_str(&row.get::<_, String>(3)?)?;
    Ok(
        json!({"name":row.get::<_,String>(0)?,"thread":row.get::<_,String>(1)?,"endpoint":row.get::<_,String>(2)?,"sources":sources,"enabled":row.get::<_,i64>(4)? != 0,"removed":row.get::<_,i64>(5)? != 0}),
    )
}

fn event_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    let envelope: Value = serde_json::from_str(&row.get::<_, String>(5)?).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(5, rusqlite::types::Type::Text, Box::new(e))
    })?;
    Ok(
        json!({"seq":row.get::<_,i64>(0)?,"id":row.get::<_,String>(1)?,"delivery_id":row.get::<_,String>(1)?,"binding":row.get::<_,String>(2)?,"source":row.get::<_,String>(3)?,"event_id":row.get::<_,String>(4)?,"envelope":envelope,"client_id":row.get::<_,String>(6)?,"state":row.get::<_,String>(7)?,"created":row.get::<_,f64>(8)?,"updated":row.get::<_,f64>(9)?,"attempts":row.get::<_,i64>(10)?,"next_at":row.get::<_,f64>(11)?,"submission_id":row.get::<_,Option<String>>(12)?,"error":row.get::<_,Option<String>>(13)?}),
    )
}

fn event_claim_row(row: &Row<'_>) -> rusqlite::Result<(String, String, f64)> {
    Ok((row.get(1)?, row.get(7)?, row.get(8)?))
}

fn event_claim_value(row: &Row<'_>) -> rusqlite::Result<Value> {
    let event = event_row(row)?;
    let envelope = event["envelope"].clone();
    Ok(
        json!({"seq":event["seq"],"id":event["id"],"delivery_id":event["delivery_id"],"binding":event["binding"],"source":event["source"],"event_id":event["event_id"],"envelope":envelope,"client_id":event["client_id"],"state":event["state"],"created":event["created"],"updated":event["updated"],"attempts":event["attempts"],"next_at":event["next_at"],"submission_id":event["submission_id"],"error":event["error"],"thread":row.get::<_,String>(14)?,"endpoint":row.get::<_,String>(15)?}),
    )
}

fn watch_value(row: &Row<'_>) -> rusqlite::Result<Value> {
    let condition: Option<String> = row.get(7)?;
    let checkpoint: Option<String> = row.get(12)?;
    let last_sample = checkpoint
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                12,
                rusqlite::types::Type::Text,
                Box::new(error),
            )
        })?
        .unwrap_or(Value::Null);
    let baseline_known = checkpoint.is_some();
    Ok(
        json!({"id":row.get::<_,String>(0)?,"thread":row.get::<_,String>(1)?,"name":row.get::<_,String>(2)?,"path":row.get::<_,String>(3)?,"interval":row.get::<_,f64>(4)?,"endpoint":row.get::<_,String>(5)?,"debounce":row.get::<_,f64>(6)?,"condition":condition.map(|s|serde_json::from_str::<Value>(&s).unwrap_or(Value::Null)),"binding":row.get::<_,String>(8)?,"enabled":row.get::<_,i64>(9)? != 0,"removed":row.get::<_,i64>(10)? != 0,"epoch":row.get::<_,i64>(11)?,"checkpoint":last_sample,"last_sample":last_sample,"baseline_known":baseline_known}),
    )
}

fn request_value(row: &Row<'_>) -> rusqlite::Result<Value> {
    let payload: Value = serde_json::from_str(&row.get::<_, String>(7)?).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(7, rusqlite::types::Type::Text, Box::new(e))
    })?;
    Ok(
        json!({"request_id":row.get::<_,String>(1)?,"thread":row.get::<_,String>(2)?,"source":row.get::<_,String>(3)?,"request_key":row.get::<_,String>(4)?,"delivery_id":row.get::<_,String>(5)?,"binding":row.get::<_,String>(6)?,"payload":payload,"state":row.get::<_,String>(8)?,"revision":row.get::<_,i64>(9)?,"expires_at":row.get::<_,Option<f64>>(10)?,"created":row.get::<_,f64>(11)?,"updated":row.get::<_,f64>(12)?}),
    )
}

fn allowed_transition(previous: &str, target: &str) -> bool {
    if matches!(previous, "completed" | "failed" | "cancelled" | "expired") {
        return false;
    }
    match target {
        "acknowledged" => previous == "received",
        "in_progress" => matches!(previous, "received" | "acknowledged"),
        "completed" | "failed" | "cancelled" | "expired" => true,
        _ => false,
    }
}
