//! Receiver process checks against a disposable store; no model is invoked.
use serde_json::{Value, json};
use std::{
    path::Path,
    process::{Command, Stdio},
    time::Duration,
};
use tempfile::TempDir;

fn cli(root: &Path, args: &[&str]) -> Value {
    let result = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .arg("--state")
        .arg(root)
        .args(args)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    serde_json::from_slice(&result.stdout).unwrap()
}
struct Receiver(std::process::Child);
impl Drop for Receiver {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[tokio::test]
async fn receiver_expires_requests_once_across_restart_without_model_calls() {
    use codex_monitor_rs::store::Store;

    let root = TempDir::new().unwrap();
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    cli(root.path(), &["init", "--port", &port.to_string()]);
    let store = Store::open(&root.path().join("rust.sqlite3")).unwrap();
    store
        .bind(
            "expiry-route",
            "expiry-thread",
            "ws://127.0.0.1:1",
            &["test".into()],
        )
        .unwrap();
    let receipt = store
        .ingest(
            "expiry-route",
            &json!({
                "id":"expiry-original", "source":"test", "type":"job.request", "data":{}
            }),
        )
        .unwrap();
    store
        .request_create(
            "expiry-thread",
            "test",
            "job",
            receipt["delivery_id"].as_str().unwrap(),
            &json!({}),
            Some(0.0),
        )
        .unwrap();
    store
        .request_create(
            "expiry-thread",
            "test",
            "quiet",
            receipt["delivery_id"].as_str().unwrap(),
            &json!({}),
            None,
        )
        .unwrap();
    for _ in 0..2 {
        let receiver = Receiver(
            Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
                .arg("--state")
                .arg(root.path())
                .arg("serve")
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap(),
        );
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            let request = store.request_get("expiry-thread", "test", "job").unwrap();
            if request["state"] == "expired" {
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "receiver did not expire request"
            );
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        // Let several maintenance ticks pass: an unchanged request must stay quiet.
        tokio::time::sleep(Duration::from_millis(1100)).await;
        let request = cli(
            root.path(),
            &[
                "request",
                "show",
                "--thread",
                "expiry-thread",
                "--source",
                "test",
                "--id",
                "job",
            ],
        );
        assert_eq!(request["state"], "expired");
        assert_eq!(request["revision"], 1);
        let quiet = store.request_get("expiry-thread", "test", "quiet").unwrap();
        assert_eq!(quiet["state"], "received");
        assert_eq!(quiet["revision"], 0);
        let db = rusqlite::Connection::open(root.path().join("rust.sqlite3")).unwrap();
        let count: i64 = db
            .query_row("SELECT count(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            count, 2,
            "only the original event and one expiry notification may exist"
        );
        drop(receiver);
    }
}

#[tokio::test]
async fn authenticated_durable_ingress_restart_and_lifecycle() {
    let root = TempDir::new().unwrap();
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    cli(root.path(), &["init", "--port", &port.to_string()]);
    cli(root.path(), &["source", "test"]);
    cli(
        root.path(),
        &[
            "bind",
            "route",
            "--thread",
            "thread-test",
            "--source",
            "test",
            "--endpoint",
            "ws://127.0.0.1:1",
        ],
    );
    let admin = std::fs::read_to_string(root.path().join("admin.token")).unwrap();
    let token = std::fs::read_to_string(root.path().join("source-test.token")).unwrap();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let url = format!("http://127.0.0.1:{port}");
    let mut receipt = Value::Null;
    for generation in 0..2 {
        let receiver = Receiver(
            Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
                .arg("--state")
                .arg(root.path())
                .arg("serve")
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap(),
        );
        let mut ready = false;
        for _ in 0..100 {
            if client
                .get(format!("{url}/v1/status"))
                .bearer_auth(&admin)
                .send()
                .await
                .is_ok()
            {
                ready = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(ready, "receiver failed to start");
        let e = json!({"id":"stable-event","source":"test","type":"test.changed","data":{"message":"hello"},"hops":0});
        let response = client
            .post(format!("{url}/v1/events/route"))
            .bearer_auth(&token)
            .json(&e)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 202);
        let value: Value = response.json().await.unwrap();
        if generation == 0 {
            receipt = value.clone();
        } else {
            assert_eq!(value["delivery_id"], receipt["delivery_id"]);
            assert_eq!(value["duplicate"], true);
        }
        let bad = client
            .post(format!("{url}/v1/events/route"))
            .bearer_auth("wrong")
            .json(&e)
            .send()
            .await
            .unwrap();
        assert_eq!(bad.status(), 401);
        let origin = client
            .post(format!("{url}/v1/events/route"))
            .bearer_auth(&token)
            .header("origin", "https://example.invalid")
            .json(&e)
            .send()
            .await
            .unwrap();
        assert_eq!(origin.status(), 403);
        let mut conflict = e.clone();
        conflict["data"] = json!({"message":"changed"});
        assert_eq!(
            client
                .post(format!("{url}/v1/events/route"))
                .bearer_auth(&token)
                .json(&conflict)
                .send()
                .await
                .unwrap()
                .status(),
            409
        );
        let event = cli(
            root.path(),
            &["event", value["delivery_id"].as_str().unwrap()],
        );
        assert_ne!(
            event["state"], "dead",
            "unavailable owner must retain pending input"
        );
        cli(root.path(), &["pause", "route"]);
        let mut paused = e.clone();
        paused["id"] = json!(format!("paused-{generation}"));
        assert_eq!(
            client
                .post(format!("{url}/v1/events/route"))
                .bearer_auth(&token)
                .json(&paused)
                .send()
                .await
                .unwrap()
                .status(),
            409
        );
        cli(root.path(), &["unpause", "route"]);
        drop(receiver);
    }
    cli(root.path(), &["pause", "route"]);
    cli(root.path(), &["unpause", "route"]);
    cli(root.path(), &["remove", "route"]);
    let event = cli(
        root.path(),
        &["event", receipt["delivery_id"].as_str().unwrap()],
    );
    assert_eq!(
        event["id"], receipt["delivery_id"],
        "removal retains evidence"
    );
}
