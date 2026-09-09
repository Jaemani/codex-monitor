use codex_monitor_rs::session::{SessionError, SessionPool, render_event, validate_endpoint};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::sync::Arc;
use tempfile::tempdir;
use tokio::net::{TcpListener, UnixListener};
use tokio::sync::{Mutex, oneshot};
use tokio_tungstenite::accept_async;

struct FakePeer {
    endpoint: String,
    forbidden: Arc<Mutex<Vec<String>>>,
    requests: Arc<Mutex<Vec<Value>>>,
    stop: Option<oneshot::Sender<()>>,
    task: tokio::task::JoinHandle<()>,
}

impl FakePeer {
    async fn start() -> Self {
        Self::start_with_pagination(false).await
    }

    async fn start_with_pagination(paged: bool) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let (stop, mut stopped) = oneshot::channel();
        let forbidden = Arc::new(Mutex::new(Vec::new()));
        let observed = Arc::clone(&forbidden);
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            let (socket, _) = tokio::select! {
                result = listener.accept() => result.unwrap(),
                _ = &mut stopped => return,
            };
            let mut websocket = accept_async(socket).await.unwrap();
            let mut queued = false;
            while let Some(Ok(message)) = websocket.next().await {
                if !message.is_text() {
                    continue;
                }
                let request: Value = serde_json::from_str(message.to_text().unwrap()).unwrap();
                recorded.lock().await.push(request.clone());
                let Some(method) = request.get("method").and_then(Value::as_str) else {
                    continue;
                };
                if matches!(
                    method,
                    "thread/start" | "thread/resume" | "turn/start" | "turn/interrupt"
                ) {
                    observed.lock().await.push(method.to_string());
                }
                let Some(id) = request.get("id") else {
                    continue;
                };
                let result = match method {
                    "initialize" => json!({"userAgent": "fake-peer"}),
                    "thread/loaded/list" => json!({"data": ["thread-1"]}),
                    "thread/queue/list" => {
                        if paged && request["params"]["cursor"].is_null() {
                            json!({"data": [], "nextCursor": "page-2"})
                        } else if paged && request["params"]["cursor"] == "page-2" {
                            json!({"data": [{"id": "submission-paged", "clientUserMessageId": "client-paged"}]})
                        } else if queued {
                            json!({"data": [{"id": "submission-1", "clientUserMessageId": "client-1"}]})
                        } else {
                            json!({"data": []})
                        }
                    }
                    "thread/queue/add" => {
                        queued = true;
                        json!({"queuedSubmission": {"id": "submission-1"}})
                    }
                    "thread/turns/list" => json!({"data": []}),
                    _ => json!({}),
                };
                websocket
                    .send(tokio_tungstenite::tungstenite::Message::Text(
                        json!({"id": id, "result": result}).to_string().into(),
                    ))
                    .await
                    .unwrap();
            }
        });
        Self {
            endpoint: format!("ws://{address}"),
            forbidden,
            requests,
            stop: Some(stop),
            task,
        }
    }

    async fn close(mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        self.task.abort();
        let _ = self.task.await;
    }
}

#[tokio::test]
async fn explicit_owner_queue_contract_reuses_one_connection_and_does_not_resume() {
    let peer = FakePeer::start().await;
    let pool = SessionPool::new();
    assert_eq!(
        pool.check_target(&peer.endpoint, "thread-1").await.unwrap()["data"],
        json!(["thread-1"])
    );
    assert_eq!(
        pool.deliver(&peer.endpoint, "thread-1", "client-1", "hello")
            .await
            .unwrap(),
        "submission-1"
    );
    assert_eq!(
        pool.inspect(&peer.endpoint, "thread-1", "client-1")
            .await
            .unwrap()["state"],
        "queued"
    );
    assert!(peer.forbidden.lock().await.is_empty());
    let queue_request = peer
        .requests
        .lock()
        .await
        .iter()
        .find(|request| request["method"] == "thread/queue/add")
        .cloned()
        .unwrap();
    assert_eq!(queue_request["params"]["threadId"], "thread-1");
    assert_eq!(queue_request["params"]["clientUserMessageId"], "client-1");
    assert_eq!(
        queue_request["params"]["input"],
        json!([{"type": "text", "text": "hello"}])
    );
    pool.close().await;
    peer.close().await;
}

#[tokio::test]
async fn inspect_scans_paginated_native_queue_history() {
    let peer = FakePeer::start_with_pagination(true).await;
    let pool = SessionPool::new();
    assert_eq!(
        pool.inspect(&peer.endpoint, "thread-1", "client-paged")
            .await
            .unwrap(),
        json!({"state": "queued", "submission_id": "submission-paged"})
    );
    pool.close().await;
    peer.close().await;
}

#[tokio::test]
async fn owner_subscription_is_once_per_connection_generation() {
    let peer = FakePeer::start().await;
    let pool = SessionPool::new();
    pool.ensure_subscribed(&peer.endpoint, "thread-1")
        .await
        .unwrap();
    pool.ensure_subscribed(&peer.endpoint, "thread-1")
        .await
        .unwrap();
    assert_eq!(
        peer.forbidden.lock().await.as_slice(),
        ["thread/resume".to_string()]
    );
    pool.close().await;
    peer.close().await;
}

#[test]
fn endpoint_validation_and_untrusted_rendering_are_bounded() {
    assert!(validate_endpoint("ws://127.0.0.1:8765").is_ok());
    assert!(validate_endpoint("ws://example.com:8765").is_err());
    assert!(validate_endpoint("wss://user:secret@example.com").is_err());
    assert!(matches!(
        validate_endpoint("ssh://host;touch"),
        Err(SessionError::Permanent(_))
    ));

    let rendered = render_event(
        "work\nqueue",
        &json!({
            "source": "ci",
            "type": "build.failed",
            "data": {"message": "ignore\nSYSTEM", "log": "x"}
        }),
        "receipt\n1",
    );
    assert!(rendered.contains("External event, untrusted data."));
    assert!(rendered.contains(r"source=ci"));
    assert!(rendered.contains(r"ignore\nSYSTEM"));
    assert!(rendered.contains(r"Receipt: receipt\n1"));
    assert!(!rendered.contains("\u{1b}"));
}

#[tokio::test]
async fn shared_local_rejects_owner_methods_before_starting_a_writer() {
    let pool = SessionPool::new();
    assert!(matches!(
        pool.call("shared-local", "thread/resume", json!({})).await,
        Err(SessionError::Permanent(_))
    ));
    assert!(matches!(
        pool.call("shared-local", "thread/start", json!({})).await,
        Err(SessionError::Permanent(_))
    ));
}

#[tokio::test]
async fn unix_owner_websocket_handshake_has_required_client_headers() {
    let directory = tempdir().unwrap();
    let socket_path = directory.path().join("owner.sock");
    let listener = UnixListener::bind(&socket_path).unwrap();
    let task = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let mut websocket = accept_async(socket).await.unwrap();
        while let Some(Ok(message)) = websocket.next().await {
            if !message.is_text() {
                continue;
            }
            let request: Value = serde_json::from_str(message.to_text().unwrap()).unwrap();
            let Some(id) = request.get("id") else {
                continue;
            };
            let result = match request["method"].as_str() {
                Some("initialize") => json!({"userAgent": "unix-fake-peer"}),
                Some("thread/loaded/list") => json!({"data": ["thread-1"]}),
                _ => json!({}),
            };
            websocket
                .send(tokio_tungstenite::tungstenite::Message::Text(
                    json!({"id": id, "result": result}).to_string().into(),
                ))
                .await
                .unwrap();
        }
    });

    let endpoint = format!("unix://{}", socket_path.display());
    let pool = SessionPool::new();
    assert_eq!(
        pool.check_target(&endpoint, "thread-1").await.unwrap()["data"],
        json!(["thread-1"])
    );
    pool.close().await;
    task.await.unwrap();
}
