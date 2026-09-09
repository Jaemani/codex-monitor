use codex_monitor_rs::{
    collector::{MAX_FILE_BYTES, run, safe_file_sample, safe_json_sample, stats},
    store::Store,
};
use serde_json::json;
use std::time::Duration;
use std::{env, fs, sync::Arc};
use tempfile::tempdir;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

#[test]
fn first_file_sample_is_a_silent_baseline_shape() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("health.json");
    fs::write(&path, br#"{"ok":true}"#).unwrap();
    let sample = safe_file_sample(&path);
    assert_eq!(sample["state"], "present");
    assert_eq!(sample["path"].as_str().unwrap(), path.to_str().unwrap());
    assert_eq!(sample["sha256"].as_str().unwrap().len(), 64);
}

#[test]
fn changed_content_has_a_different_sample_and_json_predicate() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("status.json");
    fs::write(&path, br#"{"status":{"code":3}}"#).unwrap();
    let first = safe_file_sample(&path);
    let condition = json!({"pointer":"/status/code","operator":"gte","value":3});
    let matched = safe_json_sample(&path, &condition);
    fs::write(&path, br#"{"status":{"code":1}}"#).unwrap();
    let second = safe_file_sample(&path);
    let not_matched = safe_json_sample(&path, &condition);
    assert_ne!(first["sha256"], second["sha256"]);
    assert_eq!(matched["predicate"]["state"], "matched");
    assert_eq!(not_matched["predicate"]["state"], "not_matched");
}

#[test]
fn missing_and_oversize_inputs_are_bounded() {
    let dir = tempdir().unwrap();
    let missing = safe_file_sample(&dir.path().join("recreated"));
    assert_eq!(missing["state"], "missing");
    let path = dir.path().join("large");
    let file = fs::File::create(&path).unwrap();
    file.set_len(MAX_FILE_BYTES + 1).unwrap();
    let large = safe_file_sample(&path);
    assert_eq!(large["state"], "unreadable");
    assert_eq!(large["error"], "file_too_large");
}

#[test]
fn stats_exposes_quiet_idle_counters_without_unbounded_state() {
    let snapshot = stats();
    assert_eq!(snapshot["worker_limit"], 4);
    assert!(snapshot["active_workers"].as_u64().is_some());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn native_notify_wakes_a_long_interval_watch() {
    let worker = env::var("CARGO_BIN_EXE_codex-monitor-rs")
        .expect("Cargo must provide the debug binary for the subprocess worker");
    let root = tempdir().unwrap();
    let store = Store::open(&root.path().join("monitor.sqlite3")).unwrap();
    let actual = root.path().join("actual");
    fs::create_dir(&actual).unwrap();
    let alias = root.path().join("alias");
    std::os::unix::fs::symlink(&actual, &alias).unwrap();
    let path = alias.join("sample");
    fs::write(&path, b"baseline").unwrap();
    let watch = store
        .watch_create(
            "thread",
            "native",
            path.to_str().unwrap(),
            30.0,
            "local",
            0.0,
            None,
        )
        .unwrap();
    let watch_id = watch["id"].as_str().unwrap().to_owned();
    let wake = Arc::new(Notify::new());
    let stop = CancellationToken::new();
    let old_worker = env::var_os("CODEX_MONITOR_SAMPLE_WORKER");
    // This is a debug-only regression test. The release binary is never
    // selected here; Cargo's debug target is the subprocess worker.
    unsafe {
        env::set_var("CODEX_MONITOR_SAMPLE_WORKER", worker);
    }
    let task = tokio::spawn(run(store.clone(), Arc::clone(&wake), stop.clone()));
    let mut baseline_seen = false;
    for _ in 0..30 {
        if store.checkpoint(&watch_id).unwrap().is_some() {
            baseline_seen = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(baseline_seen, "collector did not persist its baseline");
    fs::write(&path, b"changed").unwrap();
    let native_wake = tokio::time::timeout(Duration::from_secs(4), wake.notified())
        .await
        .is_ok();
    stop.cancel();
    task.await.unwrap().unwrap();
    unsafe {
        if let Some(value) = old_worker {
            env::set_var("CODEX_MONITOR_SAMPLE_WORKER", value);
        } else {
            env::remove_var("CODEX_MONITOR_SAMPLE_WORKER");
        }
    }
    let _ = fs::remove_file(&path);
    assert!(
        native_wake,
        "native notify did not wake before 30-second fallback"
    );
    let status = store.status().unwrap();
    assert_eq!(status["events"]["pending"].as_i64(), Some(1));
}
