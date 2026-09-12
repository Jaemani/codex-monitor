use codex_monitor_rs::store::Store;
use serde_json::json;
use tempfile::tempdir;

fn store() -> (tempfile::TempDir, Store) {
    let dir = tempdir().unwrap();
    let store = Store::open(&dir.path().join("rust.sqlite3")).unwrap();
    (dir, store)
}

fn envelope(id: &str) -> serde_json::Value {
    json!({"id": id, "source": "source", "type": "sample", "data": {"value": 1}})
}

#[test]
fn durable_intake_claim_and_recovery_contract() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("event-1")).unwrap();
    assert_eq!(receipt["state"], "pending");
    let claimed = store.claim().unwrap().unwrap();
    assert_eq!(claimed["state"], "submitting");
    assert!(
        claimed["client_id"]
            .as_str()
            .unwrap()
            .starts_with("codex-monitor:")
    );
    store.recover_submitting().unwrap();
    let uncertain = store.uncertain_events().unwrap();
    assert_eq!(uncertain.as_array().unwrap().len(), 1);
    assert_eq!(uncertain[0]["client_id"], claimed["client_id"]);
    store
        .finish(
            claimed["id"].as_str().unwrap(),
            "accepted",
            Some("submission-1"),
            None,
        )
        .unwrap();
    assert_eq!(
        store.event(claimed["id"].as_str().unwrap()).unwrap()["state"],
        "accepted"
    );
}

#[test]
fn fifo_dedup_and_source_auth_contract() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let first = store.ingest("route", &envelope("same")).unwrap();
    let duplicate = store.ingest("route", &envelope("same")).unwrap();
    assert!(duplicate["duplicate"].as_bool().unwrap());
    assert_eq!(duplicate["delivery_id"], first["delivery_id"]);
    assert!(
        store
            .ingest(
                "route",
                &json!({"id":"bad","source":"other","type":"sample","data":{}})
            )
            .is_err()
    );
    let second = store.ingest("route", &envelope("second")).unwrap();
    let claimed = store.claim().unwrap().unwrap();
    assert_eq!(claimed["id"], first["delivery_id"]);
    assert!(store.claim().unwrap().is_none());
    store
        .finish(
            claimed["id"].as_str().unwrap(),
            "accepted",
            Some("one"),
            None,
        )
        .unwrap();
    assert_eq!(store.claim().unwrap().unwrap()["id"], second["delivery_id"]);
}

#[test]
fn managed_epoch_checkpoint_and_event_are_atomic() {
    let (_dir, store) = store();
    let watch = store
        .watch_create(
            "thread",
            "health",
            "/tmp/health.json",
            1.0,
            "local",
            0.0,
            None,
        )
        .unwrap();
    let id = watch["id"].as_str().unwrap();
    assert!(store.checkpoint(id).unwrap().is_none());
    let event = json!({"id":"sample-1","source":"managed/file","type":"monitor.changed","data":{"ok":true}});
    let committed = store
        .commit_sample(id, 0, &json!({"last":{"ok":true}}), Some(&event))
        .unwrap();
    assert!(committed.is_some());
    assert_eq!(store.checkpoint(id).unwrap().unwrap()["last"]["ok"], true);
    let listed = store.watches(Some("thread")).unwrap();
    assert_eq!(listed[0]["baseline_known"], true);
    assert_eq!(listed[0]["last_sample"]["last"]["ok"], true);
    assert_eq!(listed[0]["checkpoint"]["last"]["ok"], true);
    store.watch_action("thread", "health", "pause").unwrap();
    store.watch_action("thread", "health", "resume").unwrap();
    assert!(
        store
            .commit_sample(id, 0, &json!({"last":{"ok":false}}), None)
            .is_err()
    );
}

#[test]
fn replies_and_requests_are_source_scoped() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("reply-parent")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();
    let reply = store.reply_put(id, "reply-1", "done").unwrap();
    let reply_id = reply["reply_id"].as_str().unwrap();
    assert_eq!(
        store.replies("source").unwrap()["data"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    store.reply_ack("source", reply_id).unwrap();
    assert!(
        store.replies("source").unwrap()["data"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let request = store
        .request_create("thread", "source", "request-1", id, &json!({"x":1}), None)
        .unwrap();
    assert_eq!(request["state"], "received");
    let updated = store
        .request_update(
            "thread",
            "source",
            "request-1",
            "update-1",
            "in_progress",
            Some(0),
            Some("working"),
        )
        .unwrap();
    assert_eq!(updated["revision"], 1);
    assert_eq!(
        store
            .requests("thread", None)
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn removing_a_route_settles_unclaimed_events_without_erasing_receipts() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("retired")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();

    store.route_action("route", "remove").unwrap();

    let event = store.event(id).unwrap();
    assert_eq!(event["state"], "dead");
    assert_eq!(event["error"], "binding removed");
    assert!(store.claim().unwrap().is_none());
    let duplicate = store.ingest("route", &envelope("retired")).unwrap();
    assert_eq!(duplicate["delivery_id"], id);
    assert_eq!(duplicate["state"], "dead");
    assert_eq!(duplicate["duplicate"], true);
}

#[test]
fn stale_completion_and_preflight_deferral_cannot_rewind_settled_events() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("stale-worker")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();
    let claimed = store.claim().unwrap().unwrap();

    store
        .finish(id, "accepted", Some("native-1"), None)
        .unwrap();
    assert!(
        store
            .finish(id, "pending", None, Some("late worker"))
            .is_err()
    );
    assert!(store.defer_unavailable(id, "late preflight").is_err());
    let event = store.event(id).unwrap();
    assert_eq!(event["state"], "accepted");
    assert_eq!(event["submission_id"], "native-1");
    assert_eq!(claimed["attempts"], 1);
}

#[test]
fn retryable_completion_is_backed_off_before_the_next_claim() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".to_owned()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("retryable")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();
    store.claim().unwrap().unwrap();
    store
        .finish(id, "pending", None, Some("temporary native rejection"))
        .unwrap();

    let event = store.event(id).unwrap();
    assert_eq!(event["state"], "pending");
    assert!(event["next_at"].as_f64().unwrap() > event["updated"].as_f64().unwrap());
    assert!(store.claim().unwrap().is_none());
}

#[test]
fn request_replay_pause_fifo_and_settlement() {
    let (_dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".into()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("parent")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();
    store
        .request_create("thread", "source", "job", id, &json!({}), None)
        .unwrap();
    store.claim().unwrap().unwrap();
    store.finish(id, "accepted", Some("parent"), None).unwrap();
    store
        .request_update(
            "thread",
            "source",
            "job",
            "start",
            "in_progress",
            Some(0),
            None,
        )
        .unwrap();
    for expected in [None, Some(0)] {
        assert_eq!(
            store
                .request_update(
                    "thread",
                    "source",
                    "job",
                    "start",
                    "in_progress",
                    expected,
                    None
                )
                .unwrap()["duplicate"],
            true
        );
    }
    assert!(
        store
            .request_update(
                "thread",
                "source",
                "job",
                "start",
                "in_progress",
                Some(1),
                None
            )
            .is_err()
    );
    store
        .request_update("thread", "source", "job", "done", "completed", None, None)
        .unwrap();
    store.route_action("route", "pause").unwrap();
    let before = store.request_get("thread", "source", "job").unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 0);
    assert_eq!(
        store.request_get("thread", "source", "job").unwrap(),
        before
    );
    store.route_action("route", "resume").unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 1);
    assert_eq!(store.request_maintenance(32).unwrap(), 0);
    let first = store.claim().unwrap().unwrap();
    store
        .finish(
            first["id"].as_str().unwrap(),
            "accepted",
            Some("first"),
            None,
        )
        .unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 1);
    let second = store.claim().unwrap().unwrap();
    store
        .finish(
            second["id"].as_str().unwrap(),
            "dead",
            None,
            Some("authentication required"),
        )
        .unwrap();
    let request = store.request_get("thread", "source", "job").unwrap();
    assert_eq!(request["history"].as_array().unwrap().len(), 2);
    assert_eq!(request["notifications"][0]["state"], "accepted");
    assert_eq!(request["notifications"][1]["state"], "failed");
    assert!(store.request_get("other", "source", "job").is_err());
}

#[test]
fn expiry_is_durable_and_paused_routes_do_not_starve_others() {
    let (dir, store) = store();
    for i in 0..6 {
        let route = format!("route-{i}");
        store
            .bind(&route, "thread", "local", &["source".into()])
            .unwrap();
        let receipt = store.ingest(&route, &envelope("parent")).unwrap();
        store
            .request_create(
                "thread",
                "source",
                &format!("job-{i}"),
                receipt["delivery_id"].as_str().unwrap(),
                &json!({}),
                Some(0.0),
            )
            .unwrap();
        if i < 5 {
            store.route_action(&route, "pause").unwrap();
        }
    }
    assert_eq!(store.request_maintenance(6).unwrap(), 6);
    assert_eq!(store.request_maintenance(1).unwrap(), 1);
    let request = store.request_get("thread", "source", "job-5").unwrap();
    assert!(request["notifications"][0]["delivery_id"].is_string());
    drop(store);
    let store = Store::open(&dir.path().join("rust.sqlite3")).unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 0);
    assert_eq!(
        store.request_get("thread", "source", "job-5").unwrap(),
        request
    );
    store.route_action("route-0", "remove").unwrap();
    assert_eq!(
        store.request_get("thread", "source", "job-0").unwrap()["notifications"][0]["state"],
        "failed"
    );
}

#[test]
fn quiet_maintenance_does_not_write_and_expired_delivery_settles_notice() {
    let (dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".into()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("parent")).unwrap();
    let id = receipt["delivery_id"].as_str().unwrap();
    store
        .request_create("thread", "source", "job", id, &json!({}), None)
        .unwrap();
    let db = rusqlite::Connection::open(dir.path().join("rust.sqlite3")).unwrap();
    let version = || {
        db.query_row("PRAGMA data_version", [], |r| r.get::<_, i64>(0))
            .unwrap()
    };
    let before = version();
    for _ in 0..10 {
        assert_eq!(store.request_maintenance(32).unwrap(), 0);
    }
    assert_eq!(version(), before, "idle maintenance committed a write");
    store.claim().unwrap().unwrap();
    store.finish(id, "accepted", Some("parent"), None).unwrap();
    store
        .request_update(
            "thread",
            "source",
            "job",
            "start",
            "in_progress",
            None,
            None,
        )
        .unwrap();
    store.request_maintenance(32).unwrap();
    db.execute("UPDATE events SET created=0 WHERE state='pending'", [])
        .unwrap();
    assert!(store.claim().unwrap().is_none());
    assert_eq!(
        store.request_get("thread", "source", "job").unwrap()["notifications"][0]["state"],
        "failed"
    );
}

#[test]
fn request_notice_waits_for_capacity_without_losing_transition() {
    let (dir, store) = store();
    store
        .bind("route", "thread", "local", &["source".into()])
        .unwrap();
    let receipt = store.ingest("route", &envelope("parent")).unwrap();
    store
        .request_create(
            "thread",
            "source",
            "job",
            receipt["delivery_id"].as_str().unwrap(),
            &json!({}),
            None,
        )
        .unwrap();
    for i in 1..120 {
        store
            .ingest("route", &envelope(&format!("fill-{i}")))
            .unwrap();
    }
    store
        .request_update("thread", "source", "job", "done", "completed", None, None)
        .unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 1);
    let request = store.request_get("thread", "source", "job").unwrap();
    assert_eq!(request["state"], "completed");
    assert!(request["notifications"][0]["delivery_id"].is_null());
    assert_eq!(request["notifications"][0]["state"], "pending");
    assert_eq!(store.request_maintenance(32).unwrap(), 0);
    let db = rusqlite::Connection::open(dir.path().join("rust.sqlite3")).unwrap();
    db.execute("UPDATE events SET created=created-61", [])
        .unwrap();
    db.execute("UPDATE request_notifications SET next_at=0", [])
        .unwrap();
    assert_eq!(store.request_maintenance(32).unwrap(), 1);
    assert!(
        store.request_get("thread", "source", "job").unwrap()["notifications"][0]["delivery_id"]
            .is_string()
    );
}
