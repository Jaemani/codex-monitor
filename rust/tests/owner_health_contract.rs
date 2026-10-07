use codex_monitor_rs::store::Store;
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::process::Command;
use std::sync::Arc;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::sync::{Mutex, oneshot};
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

#[derive(Clone, Copy)]
enum FakeOwnerMode {
    MissingAccount,
    Ready,
    ExecutionError,
    WaitingApproval,
    WaitingInput,
    WaitingBoth,
}

struct FakeOwner {
    endpoint: String,
    requests: Arc<Mutex<Vec<Value>>>,
    stop: Option<oneshot::Sender<()>>,
    task: tokio::task::JoinHandle<()>,
}

impl FakeOwner {
    async fn start(mode: FakeOwnerMode, thread: &'static str) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let (stop, mut stopped) = oneshot::channel();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            let (socket, _) = tokio::select! {
                result = listener.accept() => result.unwrap(),
                _ = &mut stopped => return,
            };
            let mut websocket = accept_async(socket).await.unwrap();
            while let Some(Ok(message)) = websocket.next().await {
                if !message.is_text() {
                    continue;
                }
                let request: Value = serde_json::from_str(message.to_text().unwrap()).unwrap();
                recorded.lock().await.push(request.clone());
                let Some(id) = request.get("id") else {
                    continue;
                };
                let method = request.get("method").and_then(Value::as_str);
                let result = match (mode, method) {
                    (_, Some("initialize")) => json!({"userAgent":"owner-health-test"}),
                    (FakeOwnerMode::MissingAccount, Some("account/read")) => {
                        json!({"account":null,"requiresOpenaiAuth":true})
                    }
                    (
                        FakeOwnerMode::Ready
                        | FakeOwnerMode::ExecutionError
                        | FakeOwnerMode::WaitingApproval
                        | FakeOwnerMode::WaitingInput
                        | FakeOwnerMode::WaitingBoth,
                        Some("account/read"),
                    ) => {
                        json!({"account":{"type":"chatgpt"},"requiresOpenaiAuth":false})
                    }
                    (
                        FakeOwnerMode::Ready
                        | FakeOwnerMode::ExecutionError
                        | FakeOwnerMode::WaitingApproval
                        | FakeOwnerMode::WaitingInput
                        | FakeOwnerMode::WaitingBoth,
                        Some("thread/loaded/list"),
                    ) => {
                        json!({"data":[thread]})
                    }
                    (FakeOwnerMode::Ready, Some("thread/read")) => {
                        json!({"status":"loaded"})
                    }
                    (
                        FakeOwnerMode::WaitingApproval
                        | FakeOwnerMode::WaitingInput
                        | FakeOwnerMode::WaitingBoth,
                        Some("thread/read"),
                    ) => {
                        let flags = match mode {
                            FakeOwnerMode::WaitingApproval => vec!["waitingOnApproval"],
                            FakeOwnerMode::WaitingInput => vec!["waitingOnUserInput"],
                            _ => vec!["waitingOnApproval", "waitingOnUserInput"],
                        };
                        json!({"thread":{"status":{"type":"active","activeFlags":flags}}})
                    }
                    (FakeOwnerMode::ExecutionError, Some("thread/read")) => {
                        json!({"thread":{"status":{"type":"systemError"}}})
                    }
                    (FakeOwnerMode::ExecutionError, Some("thread/turns/list")) => {
                        json!({"data":[{"status":"failed","error":{"message":"Your access token could not be refreshed. Bearer private-secret refresh_token=another-secret"}}]})
                    }
                    _ => json!({}),
                };
                websocket
                    .send(Message::Text(
                        json!({"id":id,"result":result}).to_string().into(),
                    ))
                    .await
                    .unwrap();
            }
        });
        Self {
            endpoint: format!("ws://{address}"),
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

fn run_cli(root: &std::path::Path, args: &[&str]) -> Value {
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

fn binding<'a>(snapshot: &'a Value, name: &str) -> &'a Value {
    snapshot["bindings"]
        .as_array()
        .unwrap()
        .iter()
        .find(|value| value["name"] == name)
        .unwrap()
}

fn assert_no_operational_methods(requests: &[Value]) {
    let methods: Vec<&str> = requests
        .iter()
        .filter_map(|request| request["method"].as_str())
        .collect();
    assert!(
        methods.iter().all(|method| {
            !method.contains("start") && !method.contains("resume") && !method.contains("model")
        }),
        "owner health sent an operational/model RPC: {methods:?}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn status_owner_health_uses_read_only_rpc_observations() {
    let auth = FakeOwner::start(FakeOwnerMode::MissingAccount, "auth-thread").await;
    let ready = FakeOwner::start(FakeOwnerMode::Ready, "ready-thread").await;
    let disconnected_listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let disconnected_port = disconnected_listener.local_addr().unwrap().port();
    drop(disconnected_listener);

    let root = TempDir::new().unwrap();
    run_cli(root.path(), &["init", "--port", "1"]);
    let store = Store::open(&root.path().join("rust.sqlite3")).unwrap();
    let source = ["test".to_string()];
    store
        .bind("auth", "auth-thread", &auth.endpoint, &source)
        .unwrap();
    store
        .bind("ready", "ready-thread", &ready.endpoint, &source)
        .unwrap();
    store
        .bind(
            "disconnected",
            "disconnected-thread",
            &format!("ws://127.0.0.1:{disconnected_port}"),
            &source,
        )
        .unwrap();
    store
        .bind("shared", "shared-thread", "shared-local", &source)
        .unwrap();

    let snapshot = run_cli(root.path(), &["status"]);
    let auth_health = &binding(&snapshot, "auth")["owner_health"];
    assert_eq!(auth_health["status"], "auth-required");
    assert_eq!(auth_health["account"]["status"], "auth-required");

    let ready_health = &binding(&snapshot, "ready")["owner_health"];
    assert_eq!(ready_health["status"], "ready-to-receive");
    assert_eq!(ready_health["ready"], true);
    assert_eq!(ready_health["thread_loaded"], true);
    assert_eq!(ready_health["credential_validation"], "unverified");
    assert_eq!(ready_health["model_execution"], "unverified");

    let disconnected_health = &binding(&snapshot, "disconnected")["owner_health"];
    assert_eq!(disconnected_health["status"], "unavailable");
    assert_eq!(disconnected_health["ready"], false);

    let shared_health = &binding(&snapshot, "shared")["owner_health"];
    assert_eq!(shared_health["endpoint"], "shared-local");
    assert_eq!(shared_health["status"], "unverified");
    assert_eq!(shared_health["ready"], false);
    assert!(
        shared_health["reason"]
            .as_str()
            .unwrap()
            .contains("explicit supported transport")
    );

    let auth_requests = auth.requests.lock().await.clone();
    let ready_requests = ready.requests.lock().await.clone();
    assert_eq!(
        auth_requests
            .iter()
            .find(|request| request["method"] == "account/read")
            .unwrap()["params"]["refreshToken"],
        false
    );
    assert!(
        ready_requests
            .iter()
            .any(|request| request["method"] == "thread/loaded/list")
    );
    assert!(
        ready_requests
            .iter()
            .any(|request| request["method"] == "thread/read")
    );
    assert_no_operational_methods(&auth_requests);
    assert_no_operational_methods(&ready_requests);

    auth.close().await;
    ready.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dashboard_reads_latest_execution_error_without_refresh_or_resume() {
    let owner = FakeOwner::start(FakeOwnerMode::ExecutionError, "failed-thread").await;
    let dir = TempDir::new().unwrap();
    run_cli(dir.path(), &["init"]);
    let store = Store::open(&dir.path().join("rust.sqlite3")).unwrap();
    store
        .bind(
            "failure",
            "failed-thread",
            &owner.endpoint,
            &["source".into()],
        )
        .unwrap();
    let snapshot = run_cli(dir.path(), &["dashboard", "--once", "--json"]);
    let b = binding(&snapshot, "failure");
    assert_eq!(b["owner_health"]["status"], "execution-error");
    assert_eq!(
        b["owner_health"]["execution_error"]["kind"],
        "authentication"
    );
    let message = b["owner_health"]["execution_error"]["message"]
        .as_str()
        .unwrap();
    assert!(message.contains("could not be refreshed"));
    assert!(!message.contains("private-secret"));
    assert!(!message.contains("another-secret"));
    assert_no_operational_methods(&owner.requests.lock().await);
    owner.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dashboard_reports_pending_decisions_without_answering_or_resuming() {
    for (mode, expected, approval, input) in [
        (
            FakeOwnerMode::WaitingApproval,
            "waiting-for-approval",
            true,
            false,
        ),
        (
            FakeOwnerMode::WaitingInput,
            "waiting-for-user-input",
            false,
            true,
        ),
        (
            FakeOwnerMode::WaitingBoth,
            "waiting-for-approval",
            true,
            true,
        ),
    ] {
        let owner = FakeOwner::start(mode, "waiting-thread").await;
        let dir = TempDir::new().unwrap();
        run_cli(dir.path(), &["init"]);
        let store = Store::open(&dir.path().join("rust.sqlite3")).unwrap();
        store
            .bind(
                "waiting",
                "waiting-thread",
                &owner.endpoint,
                &["source".into()],
            )
            .unwrap();
        let snapshot = run_cli(dir.path(), &["dashboard", "--once", "--json"]);
        let health = &binding(&snapshot, "waiting")["owner_health"];
        assert_eq!(health["status"], expected);
        assert_eq!(health["ready"], false);
        assert_eq!(health["transport_reachable"], true);
        assert_eq!(health["thread_loaded"], true);
        assert_eq!(health["attention"]["approval_required"], approval);
        assert_eq!(health["attention"]["user_input_required"], input);
        assert_eq!(health["attention"]["automatic_response"], false);
        let requests = owner.requests.lock().await;
        assert!(requests.iter().all(|r| matches!(
            r["method"].as_str(),
            Some(
                "initialize"
                    | "initialized"
                    | "account/read"
                    | "thread/loaded/list"
                    | "thread/read"
            )
        )));
        assert_no_operational_methods(&requests);
        drop(requests);
        owner.close().await;
    }
}
