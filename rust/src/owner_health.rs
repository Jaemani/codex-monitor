//! Bounded, read-only observations of an existing Codex owner.
//!
//! Owner health answers whether an explicitly addressed owner can currently
//! receive work for a loaded conversation. It never starts a local owner,
//! resumes a thread, queues input, or infers model execution.

use codex_monitor_rs::session::{SessionError, SessionPool, validate_endpoint};
use serde_json::{Value, json};
use std::collections::{BTreeSet, HashMap};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;
use tokio::time::{Instant, timeout};

const PROBE_TIMEOUT: Duration = Duration::from_millis(450);
const PROBE_BUDGET: Duration = Duration::from_secs(3);
const CACHE_TTL: Duration = Duration::from_secs(2);
const CACHE_SIZE: usize = 64;
const MAX_PAGES: usize = 8;
const MAX_THREAD_IDS: usize = 4096;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Failure {
    Auth,
    Unsupported,
    Unavailable,
}

#[derive(Clone)]
struct EndpointCache {
    expires_at: Instant,
    base: Value,
    loaded: Option<Vec<String>>,
    threads: HashMap<String, Value>,
}

#[derive(Default)]
struct CacheState {
    entries: HashMap<String, EndpointCache>,
}

pub struct OwnerHealthCache {
    state: Mutex<CacheState>,
}

impl OwnerHealthCache {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(CacheState {
                entries: HashMap::new(),
            }),
        }
    }

    async fn get(
        &self,
        pool: &SessionPool,
        endpoint: &str,
        thread: &str,
        deadline: Instant,
    ) -> Value {
        let cached = self
            .state
            .lock()
            .ok()
            .and_then(|state| state.entries.get(endpoint).cloned())
            .filter(|entry| entry.expires_at > Instant::now());
        let mut entry = if let Some(entry) = cached {
            entry
        } else {
            let mut entry = probe_endpoint(pool, endpoint, deadline).await;
            entry.expires_at = Instant::now() + CACHE_TTL;
            self.insert(endpoint, entry.clone());
            entry
        };

        if let Some(value) = entry.threads.get(thread) {
            return value.clone();
        }
        let Some(loaded) = entry.loaded.as_ref() else {
            return with_thread(&entry.base, thread);
        };
        if !loaded.iter().any(|value| value == thread) {
            let value = unloaded(&entry.base, thread, loaded);
            entry.threads.insert(thread.to_owned(), value.clone());
            self.insert(endpoint, entry);
            return value;
        }

        let value = probe_thread(pool, endpoint, thread, &entry.base, loaded, deadline).await;
        entry.threads.insert(thread.to_owned(), value.clone());
        self.insert(endpoint, entry);
        value
    }

    fn insert(&self, endpoint: &str, entry: EndpointCache) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.entries.insert(endpoint.to_owned(), entry);
        while state.entries.len() > CACHE_SIZE {
            let Some(oldest) = state
                .entries
                .iter()
                .min_by_key(|(_, value)| value.expires_at)
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            state.entries.remove(&oldest);
        }
    }
}

static GLOBAL_CACHE: OnceLock<OwnerHealthCache> = OnceLock::new();

pub fn global_cache() -> &'static OwnerHealthCache {
    GLOBAL_CACHE.get_or_init(OwnerHealthCache::new)
}

pub async fn attach(bindings: &mut Value, pool: &SessionPool) {
    let deadline = Instant::now() + PROBE_BUDGET;
    let Some(rows) = bindings.as_array_mut() else {
        return;
    };
    for binding in rows {
        let thread = binding.get("thread").and_then(Value::as_str).unwrap_or("");
        let endpoint = binding
            .get("endpoint")
            .and_then(Value::as_str)
            .unwrap_or("");
        let reason = if binding["removed"] == true {
            Some("route is removed")
        } else if binding["enabled"] != true {
            Some("route is disabled")
        } else if thread.is_empty() {
            Some("conversation identity is unavailable")
        } else if !supported_endpoint(endpoint) {
            Some("owner endpoint is not an explicit supported transport")
        } else if Instant::now() >= deadline {
            Some("owner probe budget exhausted")
        } else {
            None
        };
        binding["owner_health"] = if let Some(reason) = reason {
            base(endpoint, thread, reason)
        } else {
            global_cache().get(pool, endpoint, thread, deadline).await
        };
    }
}

fn supported_endpoint(endpoint: &str) -> bool {
    if endpoint == "shared-local" || endpoint == "local" || endpoint.starts_with("ssh://") {
        return false;
    }
    if endpoint == "unix:///" || endpoint.starts_with("unix:///") && endpoint.len() <= 9 {
        return false;
    }
    (endpoint.starts_with("ws://")
        || endpoint.starts_with("wss://")
        || endpoint.starts_with("unix://"))
        && validate_endpoint(endpoint).is_ok()
}

fn display_endpoint(endpoint: &str) -> String {
    let parsed = url::Url::parse(endpoint);
    let Ok(parsed) = parsed else {
        return endpoint.chars().take(240).collect();
    };
    if parsed.username().is_empty() && parsed.password().is_none() {
        return endpoint.chars().take(240).collect();
    }
    let host = parsed.host_str().unwrap_or("owner");
    let port = parsed
        .port()
        .map(|port| format!(":{port}"))
        .unwrap_or_default();
    format!("{}://{}{}{}", parsed.scheme(), host, port, parsed.path())
}

fn base(endpoint: &str, thread: &str, reason: &str) -> Value {
    json!({
        "endpoint": display_endpoint(endpoint),
        "thread": thread,
        "status": "unverified",
        "state": "unverified",
        "ready": false,
        "transport_reachable": false,
        "thread_loaded": Value::Null,
        "account": {"status":"unverified","present":Value::Null,"credential_validation":"unverified","refresh_token":false},
        "credential_validation": "unverified",
        "model_execution": "unverified",
        "reason": reason,
    })
}

fn with_thread(base: &Value, thread: &str) -> Value {
    let mut value = base.clone();
    value["thread"] = json!(thread);
    value
}

fn terminal(base: &Value, thread: &str, status: &str, reason: &str) -> Value {
    let mut value = with_thread(base, thread);
    value["status"] = json!(status);
    value["state"] = json!(status);
    value["ready"] = json!(false);
    value["reason"] = json!(reason);
    value
}

async fn probe_endpoint(pool: &SessionPool, endpoint: &str, deadline: Instant) -> EndpointCache {
    let mut base = base(endpoint, "", "owner unavailable");
    let account = match call(
        pool,
        endpoint,
        "account/read",
        json!({"refreshToken":false}),
        deadline,
    )
    .await
    {
        Ok(value) => {
            base["transport_reachable"] = json!(true);
            account_observation(&value)
        }
        Err(Failure::Auth) => {
            base["transport_reachable"] = json!(true);
            base["account"] = account_auth_required();
            return EndpointCache {
                expires_at: Instant::now(),
                base: terminal(&base, "", "auth-required", "authentication required"),
                loaded: None,
                threads: HashMap::new(),
            };
        }
        Err(Failure::Unsupported) => {
            base["transport_reachable"] = json!(true);
            json!({"status":"unsupported","present":Value::Null,"credential_validation":"unverified","refresh_token":false})
        }
        Err(Failure::Unavailable) => {
            return EndpointCache {
                expires_at: Instant::now(),
                base: terminal(&base, "", "unavailable", "owner unavailable"),
                loaded: None,
                threads: HashMap::new(),
            };
        }
    };
    base["account"] = account.clone();
    if account["status"] == "auth-required" {
        return EndpointCache {
            expires_at: Instant::now(),
            base: terminal(&base, "", "auth-required", "authentication required"),
            loaded: None,
            threads: HashMap::new(),
        };
    }

    let loaded = match loaded_pages(pool, endpoint, deadline).await {
        Ok(value) => value,
        Err(Failure::Auth) => {
            return EndpointCache {
                expires_at: Instant::now(),
                base: terminal(&base, "", "auth-required", "authentication required"),
                loaded: None,
                threads: HashMap::new(),
            };
        }
        Err(_) => {
            base["reason"] = json!("loaded conversations could not be read");
            return EndpointCache {
                expires_at: Instant::now(),
                base,
                loaded: None,
                threads: HashMap::new(),
            };
        }
    };
    base["transport_reachable"] = json!(true);
    EndpointCache {
        expires_at: Instant::now(),
        base,
        loaded: Some(loaded),
        threads: HashMap::new(),
    }
}

async fn probe_thread(
    pool: &SessionPool,
    endpoint: &str,
    thread: &str,
    base: &Value,
    loaded: &[String],
    deadline: Instant,
) -> Value {
    match call(
        pool,
        endpoint,
        "thread/read",
        json!({"threadId":thread,"includeTurns":false}),
        deadline,
    )
    .await
    {
        Ok(value) => {
            let mut result = with_thread(base, thread);
            result["loaded_threads"] = json!(loaded);
            result["thread_loaded"] = json!(true);
            result["thread_read"] =
                json!({"status": status_value(&value).unwrap_or_else(|| "observed".into())});
            result["status"] = json!("ready-to-receive");
            result["state"] = json!("ready-to-receive");
            result["ready"] = json!(true);
            result["reason"] = json!("owner transport reachable and conversation loaded");
            result
        }
        Err(Failure::Unsupported) => {
            let mut result = with_thread(base, thread);
            result["loaded_threads"] = json!(loaded);
            result["thread_loaded"] = json!(true);
            result["thread_read"] = json!({"status":"unsupported"});
            result["status"] = json!("ready-to-receive");
            result["state"] = json!("ready-to-receive");
            result["ready"] = json!(true);
            result["reason"] = json!("owner transport reachable and conversation loaded");
            result
        }
        Err(Failure::Auth) => thread_failure(
            base,
            thread,
            loaded,
            "auth-required",
            "authentication required",
        ),
        Err(Failure::Unavailable) => thread_failure(
            base,
            thread,
            loaded,
            "unavailable",
            "thread status unavailable",
        ),
    }
}

async fn loaded_pages(
    pool: &SessionPool,
    endpoint: &str,
    deadline: Instant,
) -> Result<Vec<String>, Failure> {
    let mut rows = BTreeSet::new();
    let mut cursor: Option<String> = None;
    let mut seen = BTreeSet::new();
    for _ in 0..MAX_PAGES {
        let params = cursor
            .as_ref()
            .map_or_else(|| json!({}), |value| json!({"cursor":value}));
        let value = call(pool, endpoint, "thread/loaded/list", params, deadline).await?;
        let Some(data) = value.get("data").and_then(Value::as_array) else {
            return Err(Failure::Unavailable);
        };
        for row in data {
            let id = row
                .as_str()
                .or_else(|| row.get("id").and_then(Value::as_str));
            if let Some(id) = id {
                rows.insert(id.to_owned());
            }
        }
        if rows.len() > MAX_THREAD_IDS {
            return Err(Failure::Unavailable);
        }
        let next = value.get("nextCursor");
        if next.is_none() || next == Some(&Value::Null) {
            return Ok(rows.into_iter().collect());
        }
        let Some(next) = next.and_then(Value::as_str) else {
            return Err(Failure::Unavailable);
        };
        if !seen.insert(next.to_owned()) {
            return Err(Failure::Unavailable);
        }
        cursor = Some(next.to_owned());
    }
    Err(Failure::Unavailable)
}

async fn call(
    pool: &SessionPool,
    endpoint: &str,
    method: &str,
    params: Value,
    deadline: Instant,
) -> Result<Value, Failure> {
    let remaining = deadline
        .saturating_duration_since(Instant::now())
        .min(PROBE_TIMEOUT);
    if remaining.is_zero() {
        return Err(Failure::Unavailable);
    }
    match timeout(remaining, pool.call(endpoint, method, params)).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(error)) => Err(classify_error(&error)),
        Err(_) => Err(Failure::Unavailable),
    }
}

fn classify_error(error: &SessionError) -> Failure {
    let text = error.to_string().to_lowercase();
    if [
        "auth",
        "credential",
        "forbidden",
        "invalid token",
        "logged out",
        "login",
        "unauthorized",
    ]
    .iter()
    .any(|word| text.contains(word))
    {
        Failure::Auth
    } else if text.contains("method not found")
        || text.contains("unsupported")
        || text.contains("unknown variant")
        || text.contains("rejected: ") && text.contains("method")
    {
        Failure::Unsupported
    } else {
        Failure::Unavailable
    }
}

fn account_auth_required() -> Value {
    json!({"status":"auth-required","present":false,"credential_validation":"unverified","refresh_token":false})
}

fn account_observation(value: &Value) -> Value {
    let Some(object) = value.as_object() else {
        return json!({"status":"unknown","present":Value::Null,"credential_validation":"unverified","refresh_token":false});
    };
    let account = object.get("account");
    let requires_auth = object.get("requiresOpenaiAuth").and_then(Value::as_bool);
    if account.is_none_or(Value::is_null) && requires_auth == Some(true) {
        return account_auth_required();
    }
    if account.is_none_or(Value::is_null) && requires_auth != Some(false) {
        return json!({"status":"unknown","present":Value::Null,"credential_validation":"unverified","refresh_token":false});
    }
    let present = account.is_some_and(truthy);
    json!({"status":if present {"present"} else {"absent"},"present":present,"credential_validation":"unverified","refresh_token":false})
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|number| number != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn status_value(value: &Value) -> Option<String> {
    if let Some(value) = value.as_str() {
        let value = value.trim();
        if !value.is_empty() && value.len() <= 64 {
            return Some(
                value
                    .chars()
                    .map(|character| {
                        if character.is_control() {
                            ' '
                        } else {
                            character
                        }
                    })
                    .collect(),
            );
        }
    }
    value.as_object().and_then(|object| {
        ["type", "status", "state"]
            .iter()
            .find_map(|key| object.get(*key).and_then(status_value))
    })
}

fn unloaded(base: &Value, thread: &str, loaded: &[String]) -> Value {
    let mut value = terminal(
        base,
        thread,
        "unloaded",
        "conversation is not loaded by this owner",
    );
    value["loaded_threads"] = json!(loaded);
    value["thread_loaded"] = json!(false);
    value
}

fn thread_failure(
    base: &Value,
    thread: &str,
    loaded: &[String],
    status: &str,
    reason: &str,
) -> Value {
    let mut value = terminal(base, thread, status, reason);
    value["loaded_threads"] = json!(loaded);
    value["thread_loaded"] = json!(true);
    value
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_explicit_supported_owner_transports_are_probeable() {
        assert!(supported_endpoint("ws://127.0.0.1:8765"));
        assert!(supported_endpoint("wss://owner.example"));
        assert!(supported_endpoint("unix:///tmp/owner.sock"));
        assert!(!supported_endpoint("shared-local"));
        assert!(!supported_endpoint("local"));
        assert!(!supported_endpoint("ssh://owner"));
        assert!(!supported_endpoint("ws://example.com:8765"));
        assert!(!supported_endpoint("wss://user:secret@owner.example"));
    }

    #[test]
    fn account_presence_never_claims_credential_validation() {
        let value = account_observation(&json!({"account":{"type":"chatgpt"}}));
        assert_eq!(value["status"], "present");
        assert_eq!(value["credential_validation"], "unverified");
        assert_eq!(value["refresh_token"], false);
        let auth = account_observation(&json!({"account":null,"requiresOpenaiAuth":true}));
        assert_eq!(auth["status"], "auth-required");
        assert_eq!(auth["present"], false);
        let unknown = account_observation(&json!({}));
        assert_eq!(unknown["status"], "unknown");
        assert!(unknown["present"].is_null());
    }

    #[test]
    fn endpoint_display_redacts_credentials() {
        let value = base(
            "wss://user:secret@owner.example:8765/path",
            "thread",
            "test",
        );
        assert_eq!(value["endpoint"], "wss://owner.example:8765/path");
        assert!(!value.to_string().contains("secret"));
    }
}
