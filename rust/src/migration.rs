//! One-shot, offline adoption of a Python monitor state directory.
use anyhow::{Context, Result, bail};
use codex_monitor_rs::store::Store;
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::path::Path;

pub fn migrate(root: &Path) -> Result<Value> {
    let _receiver =
        crate::lock(root, "serve.lock").context("Stop the receiver before migration")?;
    let _config = crate::lock(root, "config.lock")?;
    let original = std::fs::read(root.join("config.json"))?;
    let mut config: Value = serde_json::from_slice(&original)?;
    if config["version"] != 1 || !config["port"].as_u64().is_some_and(|p| p > 0 && p <= 65535) {
        bail!("Unsupported legacy configuration; migration cancelled");
    }
    if config["limits"]
        .as_object()
        .is_some_and(|limits| !limits.is_empty())
    {
        bail!(
            "Custom legacy limits require explicit mapping before migration; no limits were silently discarded"
        );
    }
    if config["runtime"] == "rust" {
        bail!("State is already adopted by Rust; no data was changed");
    }
    if root.join("rust.sqlite3").exists() {
        bail!("Rust database already exists; inspect previous migration before retrying");
    }
    let staging = root.join(format!(".migration-{}.sqlite3", uuid::Uuid::new_v4()));
    let backup = root
        .join("migration-backups")
        .join(uuid::Uuid::new_v4().to_string());
    std::fs::create_dir_all(&backup)?;
    crate::private_write(&backup.join("config.json"), &original)?;
    // SQLite VACUUM INTO includes committed WAL pages, unlike a raw file copy.
    for name in ["monitor.sqlite3", "replies.sqlite3", "requests.sqlite3"] {
        let path = root.join(name);
        if !path.exists() {
            continue;
        }
        let source =
            Connection::open_with_flags(&path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        source.execute(
            "VACUUM INTO ?",
            [backup.join(name).to_string_lossy().as_ref()],
        )?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(backup.join(name), std::fs::Permissions::from_mode(0o600))?;
    }
    let result = (|| -> Result<Value> {
        drop(Store::open(&staging)?);
        let mut db = Connection::open(&staging)?;
        db.execute(
            "ATTACH DATABASE ? AS old",
            [backup.join("monitor.sqlite3").to_string_lossy().as_ref()],
        )?;
        let tx = db.transaction()?;
        let mut counts = serde_json::Map::new();
        for table in ["bindings", "conversation_metadata", "events"] {
            tx.execute(
                &format!("INSERT INTO main.{table} SELECT * FROM old.{table}"),
                [],
            )?;
            let old: i64 = tx.query_row(&format!("SELECT count(*) FROM old.{table}"), [], |r| {
                r.get(0)
            })?;
            let new: i64 =
                tx.query_row(&format!("SELECT count(*) FROM main.{table}"), [], |r| {
                    r.get(0)
                })?;
            if old != new {
                bail!("Migration count mismatch: {table}");
            }
            counts.insert(table.into(), json!(new));
        }
        // Keep historical decisions queryable even though dispatch does not use them.
        tx.execute_batch("CREATE TABLE legacy_decisions AS SELECT * FROM old.decisions;")?;
        tx.execute_batch("INSERT INTO managed_watches(id,thread,name,path,interval,endpoint,debounce,condition,binding,enabled,removed,epoch,created,updated) SELECT w.id,w.thread,w.name,w.path,w.interval,b.endpoint,w.debounce_seconds,w.condition_json,w.binding,w.enabled,w.removed,w.lifecycle_epoch,w.created,w.updated FROM old.managed_watches w JOIN old.bindings b ON b.name=w.binding;")?;
        let watches: Vec<(String, Option<String>)> = tx
            .prepare("SELECT id,condition FROM managed_watches")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<std::result::Result<_, _>>()?;
        for (id, condition) in &watches {
            if id.contains('/') || id.contains("..") {
                bail!("Unsafe watch identifier");
            }
            let path = root.join("managed").join(format!("{id}.json"));
            let debounce_path = root.join("managed").join(format!("{id}.condition.json"));
            if debounce_path.exists() {
                crate::private_write(
                    &backup.join(debounce_path.file_name().context("debounce checkpoint")?),
                    &std::fs::read(&debounce_path)?,
                )?;
            }
            if path.exists() {
                let raw = std::fs::read(&path)?;
                let checkpoint: Value = serde_json::from_slice(&raw)?;
                if checkpoint.get("pending").is_some_and(|v| !v.is_null()) {
                    bail!(
                        "A watch has an uncommitted pending event; reconcile it before migration"
                    );
                }
                // Preserve the source checkpoint as an audit artifact.
                crate::private_write(
                    &backup.join(path.file_name().context("checkpoint filename")?),
                    &raw,
                )?;
                let last = checkpoint
                    .get("last")
                    .context("Unsupported legacy checkpoint; migration cancelled")?;
                let translated = last.clone();
                if condition.is_some() && !translated["condition"].is_string() {
                    bail!("Unsupported condition baseline");
                }
                tx.execute(
                    "UPDATE managed_watches SET checkpoint=? WHERE id=?",
                    params![serde_json::to_string(&translated)?, id],
                )?;
            }
        }
        counts.insert("managed_watches".into(), json!(watches.len()));
        tx.commit()?;
        for (name, alias) in [
            ("replies.sqlite3", "reply"),
            ("requests.sqlite3", "request"),
        ] {
            if !backup.join(name).exists() {
                continue;
            }
            db.execute(
                &format!("ATTACH DATABASE ? AS {alias}"),
                [backup.join(name).to_string_lossy().as_ref()],
            )?;
            let tx = db.transaction()?;
            if alias == "reply" {
                tx.execute_batch("INSERT INTO main.replies SELECT * FROM reply.replies;")?;
            } else {
                tx.execute_batch("INSERT INTO main.requests(seq,request_id,thread,source,request_key,delivery_id,binding,payload,state,revision,expires_at,created,updated) SELECT seq,request_id,conversation_id,source,request_key,original_delivery_id,original_binding,payload,state,revision,expires_at,created,updated FROM request.requests;
                INSERT INTO main.request_updates(seq,request_id,update_id,expected_revision,revision,previous_state,state,detail,created) SELECT seq,request_id,update_id,expected_revision,revision,previous_state,state,summary,created FROM request.request_updates;
                INSERT INTO main.request_notifications SELECT * FROM request.request_notifications;
                CREATE TABLE legacy_request_expiry AS SELECT request_id,expires_in FROM request.requests;")?;
            }
            for table in if alias == "reply" {
                vec!["replies"]
            } else {
                vec!["requests", "request_updates", "request_notifications"]
            } {
                let n: i64 =
                    tx.query_row(&format!("SELECT count(*) FROM main.{table}"), [], |r| {
                        r.get(0)
                    })?;
                let old: i64 =
                    tx.query_row(&format!("SELECT count(*) FROM {alias}.{table}"), [], |r| {
                        r.get(0)
                    })?;
                if n != old {
                    bail!("Migration count mismatch: {table}");
                }
                counts.insert(table.into(), json!(n));
            }
            tx.commit()?;
        }
        let integrity: String = db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
        if integrity != "ok" {
            bail!("Migrated database integrity check failed");
        }
        db.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
        drop(db);
        std::fs::rename(&staging, root.join("rust.sqlite3"))?;
        config["runtime"] = json!("rust");
        config["migration_backup"] = json!(backup);
        crate::private_write(
            &root.join("config.json"),
            &serde_json::to_vec_pretty(&config)?,
        )?;
        let report = json!({"runtime":"rust","backup":backup,"counts":counts,"credentials":"preserved","input_replayed":false});
        crate::private_write(
            &backup.join("report.json"),
            &serde_json::to_vec_pretty(&report)?,
        )?;
        Ok(report)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&staging);
    }
    result
}
