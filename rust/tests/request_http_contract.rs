//! Authenticated source-scoped request HTTP contract checks.
use codex_monitor_rs::store::Store;
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

async fn start_receiver(
    root: &Path,
    port: u16,
    admin: &str,
) -> (Receiver, reqwest::Client, String) {
    let receiver = Receiver(
        Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
            .arg("--state")
            .arg(root)
            .arg("serve")
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let url = format!("http://127.0.0.1:{port}");
    for _ in 0..100 {
        if client
            .get(format!("{url}/v1/status"))
            .bearer_auth(admin)
            .send()
            .await
            .is_ok()
        {
            return (receiver, client, url);
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("receiver failed to start");
}

#[tokio::test]
async fn request_http_lifecycle_is_source_scoped_and_paginated() {
    let root = TempDir::new().unwrap();
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    cli(root.path(), &["init", "--port", &port.to_string()]);
    cli(root.path(), &["source", "build"]);
    cli(root.path(), &["source", "other"]);
    cli(
        root.path(),
        &[
            "bind",
            "build-route",
            "--thread",
            "thread-build",
            "--source",
            "build",
            "--endpoint",
            "ws://127.0.0.1:1",
        ],
    );
    cli(
        root.path(),
        &[
            "bind",
            "other-route",
            "--thread",
            "thread-other",
            "--source",
            "other",
            "--endpoint",
            "ws://127.0.0.1:1",
        ],
    );
    cli(
        root.path(),
        &[
            "bind",
            "build-second-route",
            "--thread",
            "thread-build",
            "--source",
            "build",
            "--endpoint",
            "ws://127.0.0.1:1",
        ],
    );
    cli(
        root.path(),
        &[
            "bind",
            "build-other-thread-route",
            "--thread",
            "thread-build-two",
            "--source",
            "build",
            "--endpoint",
            "ws://127.0.0.1:1",
        ],
    );
    let admin = std::fs::read_to_string(root.path().join("admin.token")).unwrap();
    let build_token = std::fs::read_to_string(root.path().join("source-build.token")).unwrap();
    let other_token = std::fs::read_to_string(root.path().join("source-other.token")).unwrap();
    let store = Store::open(&root.path().join("rust.sqlite3")).unwrap();
    let build_delivery = store
        .ingest(
            "build-route",
            &json!({"id":"build-event","source":"build","type":"build.failed","data":{}}),
        )
        .unwrap()["delivery_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let other_delivery = store
        .ingest(
            "other-route",
            &json!({"id":"other-event","source":"other","type":"build.failed","data":{}}),
        )
        .unwrap()["delivery_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let build_second_delivery = store
        .ingest(
            "build-second-route",
            &json!({"id":"build-event-2","source":"build","type":"build.failed","data":{}}),
        )
        .unwrap()["delivery_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let build_other_thread_delivery = store
        .ingest(
            "build-other-thread-route",
            &json!({"id":"build-event-3","source":"build","type":"build.failed","data":{}}),
        )
        .unwrap()["delivery_id"]
        .as_str()
        .unwrap()
        .to_owned();
    drop(store);

    let (_receiver, client, url) = start_receiver(root.path(), port, &admin).await;
    let response = client
        .post(format!("{url}/v1/requests"))
        .bearer_auth(&build_token)
        .json(&json!({
            "delivery_id": build_delivery,
            "request_key": "build-status",
            "payload": {"job": 17},
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    let created: Value = response.json().await.unwrap();
    assert_eq!(created["state"], "received");
    assert_eq!(
        created["capacity"],
        json!({
            "updates_used": 0,
            "updates_limit": 64,
            "updates_remaining": 64,
        })
    );
    let request_id = created["request_id"].as_str().unwrap().to_owned();

    let response = client
        .post(format!("{url}/v1/requests"))
        .bearer_auth(&other_token)
        .json(&json!({
            "delivery_id": other_delivery,
            "request_key": "other-status",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);

    let response = client
        .post(format!("{url}/v1/requests"))
        .bearer_auth(&build_token)
        .json(&json!({
            "delivery_id": build_second_delivery,
            "request_key": "build-status-2",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);
    let second_created: Value = response.json().await.unwrap();
    let second_request_id = second_created["request_id"].as_str().unwrap().to_owned();

    let response = client
        .post(format!("{url}/v1/requests"))
        .bearer_auth(&build_token)
        .json(&json!({
            "delivery_id": build_other_thread_delivery,
            "request_key": "build-other-thread",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 201);

    let response = client
        .get(format!("{url}/v1/requests?thread=thread-build&limit=1"))
        .bearer_auth(&build_token)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let listed: Value = response.json().await.unwrap();
    assert_eq!(listed["data"][0]["request_id"], request_id);
    let next = listed["next"].as_str().unwrap().to_owned();
    let response = client
        .get(format!(
            "{url}/v1/requests?thread=thread-build&limit=1&after={next}"
        ))
        .bearer_auth(&build_token)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let second_page: Value = response.json().await.unwrap();
    assert_eq!(second_page["data"][0]["request_id"], second_request_id);
    assert!(second_page["next"].is_null());

    let response = client
        .get(format!("{url}/v1/requests?thread=thread-build-two"))
        .bearer_auth(&build_token)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let other_thread: Value = response.json().await.unwrap();
    assert_eq!(other_thread["data"].as_array().unwrap().len(), 1);

    let response = client
        .get(format!("{url}/v1/requests/{request_id}"))
        .bearer_auth(&build_token)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let current: Value = response.json().await.unwrap();
    assert_eq!(current["revision"], 0);

    let response = client
        .post(format!("{url}/v1/requests/{request_id}/updates"))
        .bearer_auth(&build_token)
        .json(&json!({
            "update_id": "started",
            "state": "in_progress",
            "expected_revision": 0,
            "detail": "Started build",
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let updated: Value = response.json().await.unwrap();
    assert_eq!(updated["state"], "in_progress");
    assert_eq!(updated["revision"], 1);
    assert_eq!(updated["capacity"]["updates_used"], 1);

    assert_eq!(
        client
            .get(format!("{url}/v1/requests/{request_id}"))
            .bearer_auth(&other_token)
            .send()
            .await
            .unwrap()
            .status(),
        404
    );
    assert_eq!(
        client
            .post(format!("{url}/v1/requests"))
            .bearer_auth(&build_token)
            .json(&json!({
                "delivery_id": other_delivery,
                "request_key": "cross-source",
            }))
            .send()
            .await
            .unwrap()
            .status(),
        404
    );
    assert_eq!(
        client
            .get(format!("{url}/v1/requests?thread=thread-build&limit=0"))
            .bearer_auth(&build_token)
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
    assert_eq!(
        client
            .get(format!("{url}/v1/requests?thread=thread-build&unknown=x"))
            .bearer_auth(&build_token)
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
    assert_eq!(
        client
            .post(format!("{url}/v1/requests"))
            .bearer_auth(&build_token)
            .json(&json!({
                "delivery_id": build_delivery,
                "request_key": "unknown-body-field",
                "unexpected": true,
            }))
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
    assert_eq!(
        client
            .post(format!("{url}/v1/requests/{request_id}/updates"))
            .bearer_auth(&build_token)
            .json(&json!({
                "update_id": "unexpected-field",
                "state": "completed",
                "expected_revision": 1,
                "unexpected": true,
            }))
            .send()
            .await
            .unwrap()
            .status(),
        400
    );
    assert_eq!(
        client
            .post(format!("{url}/v1/requests"))
            .bearer_auth(&admin)
            .json(&json!({
                "delivery_id": build_delivery,
                "request_key": "admin-forged",
            }))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
}
