use crate::{Config, now, output, owner_health, text, token};
use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State},
    http::{HeaderMap, StatusCode, Uri},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use codex_monitor_rs::{
    collector,
    session::{SessionPool, render_event},
    store::Store,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path as FsPath, PathBuf},
    sync::Arc,
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;

pub fn client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .redirect(reqwest::redirect::Policy::none())
        .build()?)
}
pub fn validate_ingress_url(raw: &str) -> Result<()> {
    let u = url::Url::parse(raw)?;
    if !u.username().is_empty()
        || u.password().is_some()
        || u.fragment().is_some()
        || u.query().is_some()
    {
        bail!("ingress URL cannot contain credentials, query or fragment");
    }
    if u.scheme() != "https"
        && !(u.scheme() == "http"
            && matches!(u.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")))
    {
        bail!("use HTTPS or loopback HTTP for event ingress");
    }
    Ok(())
}
pub async fn health(c: &Config, root: &FsPath) -> Value {
    let result = async {
        let admin = token(&root.join("admin.token"))?;
        let r = client()?
            .get(format!("http://127.0.0.1:{}/v1/status", c.port))
            .bearer_auth(admin)
            .timeout(Duration::from_millis(500))
            .send()
            .await?;
        if !r.status().is_success() {
            bail!("receiver unavailable");
        }
        Ok::<Value, anyhow::Error>(r.json().await?)
    }
    .await;
    result.unwrap_or_else(|_| json!({"ready":false,"reason":"receiver is not verified reachable"}))
}
pub async fn snapshot(
    store: &Store,
    c: &Config,
    root: &FsPath,
    pool: &SessionPool,
) -> Result<Value> {
    let mut bindings = store.bindings()?;
    owner_health::attach(&mut bindings, pool).await;
    Ok(
        json!({"runtime":"rust","generated_at":now(),"receiver":health(c,root).await,"bindings":bindings,"conversations":store.metadata_list(None)?,"monitors":store.watches(None)?,"delivery":store.status()?,"scope":{"event_delivery":"persisted_observations","model_telemetry":"not_collected","tool_telemetry":"not_collected"}}),
    )
}
#[derive(Clone)]
struct App {
    store: Store,
    sources: Arc<BTreeMap<String, String>>,
    admin: Arc<String>,
    wake: Arc<Notify>,
    slots: Arc<Semaphore>,
}
#[derive(Debug)]
struct ApiError(StatusCode, &'static str);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
fn storage_error(e: anyhow::Error) -> ApiError {
    // Stable public categories; never expose DB paths or event/credential bodies.
    let s = e.to_string().to_lowercase();
    if s.contains("paused") || s.contains("disabled") || s.contains("removed") {
        ApiError(StatusCode::CONFLICT, "target lifecycle conflict")
    } else if s.contains("conflict")
        || s.contains("different")
        || s.contains("duplicate")
        || s.contains("exists")
        || s.contains("state transition is not allowed")
    {
        ApiError(StatusCode::CONFLICT, "identifier conflict")
    } else if s.contains("capacity") || s.contains("rate") {
        ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "capacity or rate limit reached",
        )
    } else if s.contains("unknown request state") {
        ApiError(
            StatusCode::BAD_REQUEST,
            "invalid or unavailable target/input",
        )
    } else if s.contains("not found") || s.contains("unknown") {
        ApiError(StatusCode::NOT_FOUND, "resource not found")
    } else if s.contains("invalid")
        || s.contains("must")
        || s.contains("exceed")
        || s.contains("empty")
        || s.contains("too long")
        || s.contains("finite")
        || s.contains("malformed")
        || s.contains("paused")
        || s.contains("removed")
        || s.contains("disabled")
        || s.contains("source")
    {
        ApiError(
            StatusCode::BAD_REQUEST,
            "invalid or unavailable target/input",
        )
    } else {
        ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "persistence unavailable; retry with the same event id",
        )
    }
}
fn bearer(headers: &HeaderMap) -> &str {
    headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("")
}
fn same(a: &str, b: &str) -> bool {
    a.as_bytes().ct_eq(b.as_bytes()).into()
}
fn source(app: &App, h: &HeaderMap) -> Result<String, ApiError> {
    app.sources
        .iter()
        .find(|(_, secret)| same(secret, bearer(h)))
        .map(|(name, _)| name.clone())
        .ok_or(ApiError(
            StatusCode::UNAUTHORIZED,
            "source credentials required",
        ))
}
fn admin(app: &App, h: &HeaderMap) -> Result<(), ApiError> {
    if same(&app.admin, bearer(h)) {
        Ok(())
    } else {
        Err(ApiError(
            StatusCode::UNAUTHORIZED,
            "admin credentials required",
        ))
    }
}
async fn guard(State(app): State<App>, req: Request, next: Next) -> Response {
    if req.headers().contains_key("origin") {
        return ApiError(StatusCode::FORBIDDEN, "browser origins are not accepted").into_response();
    }
    let Ok(_permit) = app.slots.clone().try_acquire_owned() else {
        return ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "receiver busy; retry with the same event id",
        )
        .into_response();
    };
    if req.method() == axum::http::Method::POST {
        if req.headers().contains_key("transfer-encoding") {
            return ApiError(
                StatusCode::BAD_REQUEST,
                "chunked requests are not supported",
            )
            .into_response();
        }
        let len = req
            .headers()
            .get("content-length")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<usize>().ok());
        if len.is_none() {
            return ApiError(StatusCode::LENGTH_REQUIRED, "Content-Length required")
                .into_response();
        }
        if len.unwrap() > 32768 {
            return ApiError(StatusCode::PAYLOAD_TOO_LARGE, "body exceeds 32 KiB").into_response();
        }
        if !req.uri().path().ends_with("/ack")
            && req
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").trim())
                != Some("application/json")
        {
            return ApiError(
                StatusCode::UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
            .into_response();
        }
    }
    match tokio::time::timeout(Duration::from_secs(5), next.run(req)).await {
        Ok(mut response) => {
            response.headers_mut().insert(
                "cache-control",
                axum::http::HeaderValue::from_static("no-store"),
            );
            response
        }
        Err(_) => ApiError(StatusCode::REQUEST_TIMEOUT, "request timed out").into_response(),
    }
}
async fn ingest(
    State(app): State<App>,
    Path(binding): Path<String>,
    h: HeaderMap,
    Json(e): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = source(&app, &h)?;
    if e["source"] != name {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "credential source does not match envelope",
        ));
    }
    let r = app.store.ingest(&binding, &e).map_err(storage_error)?;
    app.wake.notify_one();
    Ok((StatusCode::ACCEPTED, Json(r)))
}
async fn status(State(app): State<App>, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    admin(&app, &h)?;
    Ok(Json(
        json!({"ready":true,"runtime":"rust","delivery":app.store.status().map_err(storage_error)?,"collector":collector::stats(),"capabilities":{"managed_file_monitor":true,"managed_json_predicates":true,"reply_outbox":true,"request_lifecycle":true,"request_cli":true,"request_maintenance":true,"request_http":true},"consumer_ready":"unknown","generated_at":now()}),
    ))
}
async fn sessions(State(app): State<App>, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    admin(&app, &h)?;
    Ok(Json(
        json!({"bindings":app.store.bindings().map_err(storage_error)?,"consumer_ready":"unknown"}),
    ))
}
async fn delivery(
    State(app): State<App>,
    Path(id): Path<String>,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    admin(&app, &h)?;
    Ok(Json(app.store.event(&id).map_err(storage_error)?))
}
async fn replies(State(app): State<App>, h: HeaderMap) -> Result<Json<Value>, ApiError> {
    let name = source(&app, &h)?;
    Ok(Json(app.store.replies(&name).map_err(storage_error)?))
}
async fn ack(
    State(app): State<App>,
    Path(id): Path<String>,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let name = source(&app, &h)?;
    if h.get("content-length").and_then(|v| v.to_str().ok()) != Some("0") {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "acknowledgement takes an empty body",
        ));
    }
    Ok(Json(
        app.store.reply_ack(&name, &id).map_err(storage_error)?,
    ))
}

const HTTP_REQUEST_UPDATES_LIMIT: i64 = 64;

fn ensure_body_fields(
    body: &serde_json::Map<String, Value>,
    allowed: &[&str],
) -> Result<(), ApiError> {
    if body
        .keys()
        .any(|field| !allowed.iter().any(|name| *name == field))
    {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "unknown request body field",
        ));
    }
    Ok(())
}

fn query_values(uri: &Uri, allowed: &[&str]) -> Result<BTreeMap<String, String>, ApiError> {
    let mut values = BTreeMap::new();
    let Some(query) = uri.query() else {
        return Ok(values);
    };
    for (key, value) in url::form_urlencoded::parse(query.as_bytes()) {
        let key = key.into_owned();
        if !allowed.iter().any(|name| *name == key) {
            return Err(ApiError(StatusCode::BAD_REQUEST, "unknown query parameter"));
        }
        if values.insert(key, value.into_owned()).is_some() {
            return Err(ApiError(
                StatusCode::BAD_REQUEST,
                "query parameter must occur once",
            ));
        }
    }
    Ok(values)
}

fn request_body(value: &Value) -> Result<&serde_json::Map<String, Value>, ApiError> {
    value.as_object().ok_or(ApiError(
        StatusCode::BAD_REQUEST,
        "request body must be an object",
    ))
}

fn request_string<'a>(
    body: &'a serde_json::Map<String, Value>,
    field: &'static str,
) -> Result<&'a str, ApiError> {
    body.get(field).and_then(Value::as_str).ok_or(ApiError(
        StatusCode::BAD_REQUEST,
        "request field must be a string",
    ))
}

fn valid_request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 200
        && value.bytes().enumerate().all(|(index, byte)| {
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:@/-".contains(&byte)
                && (index != 0 || byte.is_ascii_alphanumeric())
        })
}

fn optional_expiry(body: &serde_json::Map<String, Value>) -> Result<Option<f64>, ApiError> {
    match body.get("expires_at") {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0)
            .map(Some)
            .ok_or(ApiError(StatusCode::BAD_REQUEST, "expiry must be finite")),
    }
}

fn request_capacity(value: &Value) -> Result<Value> {
    let updates_used = value
        .get("revision")
        .and_then(Value::as_i64)
        .context("request revision missing")?;
    Ok(json!({
        "updates_used": updates_used,
        "updates_limit": HTTP_REQUEST_UPDATES_LIMIT,
        "updates_remaining": (HTTP_REQUEST_UPDATES_LIMIT - updates_used).max(0),
    }))
}

fn request_view(mut value: Value) -> Result<Value> {
    value["capacity"] = request_capacity(&value)?;
    Ok(value)
}

fn request_by_id(store: &Store, source: &str, request_id: &str) -> Result<Option<Value>> {
    match store.request_get_by_id(request_id, source) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.to_string().to_lowercase().contains("not found") => Ok(None),
        Err(error) => Err(error),
    }
}

fn request_record_by_id(store: &Store, source: &str, request_id: &str) -> Result<Option<Value>> {
    match store.request_record_by_id(request_id, source) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.to_string().to_lowercase().contains("not found") => Ok(None),
        Err(error) => Err(error),
    }
}

fn request_list(
    store: &Store,
    thread: &str,
    source: &str,
    limit: usize,
    after: Option<&str>,
) -> Result<Value> {
    let cursor = after
        .map(|value| value.parse::<i64>())
        .transpose()
        .context("request cursor must be a nonnegative integer")?;
    let page = store.request_page(thread, source, limit, cursor)?;
    let values = page
        .get("data")
        .and_then(Value::as_array)
        .context("request list is not an array")?;
    let mut data = Vec::with_capacity(values.len());
    for value in values {
        data.push(request_view(value.clone())?);
    }
    Ok(json!({
        "data": data,
        "next": page.get("next").cloned().unwrap_or(Value::Null),
    }))
}

async fn request_create(
    State(app): State<App>,
    uri: Uri,
    h: HeaderMap,
    Json(body): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = source(&app, &h)?;
    query_values(&uri, &[])?;
    let body = request_body(&body)?;
    ensure_body_fields(
        body,
        &["delivery_id", "request_key", "payload", "expires_at"],
    )?;
    let delivery_id = request_string(body, "delivery_id")?;
    let request_key = request_string(body, "request_key")?;
    let payload = body.get("payload").cloned().unwrap_or_else(|| json!({}));
    if !payload.is_object() {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "request payload must be an object",
        ));
    }
    let expires_at = optional_expiry(body)?;
    let event = app.store.event(delivery_id).map_err(storage_error)?;
    if event.get("source").and_then(Value::as_str) != Some(name.as_str()) {
        return Err(ApiError(StatusCode::NOT_FOUND, "delivery not found"));
    }
    let binding = event
        .get("binding")
        .and_then(Value::as_str)
        .context("delivery binding missing")
        .map_err(|_| ApiError(StatusCode::NOT_FOUND, "delivery binding not found"))?;
    let bindings = app.store.bindings().map_err(storage_error)?;
    let thread = bindings
        .as_array()
        .into_iter()
        .flatten()
        .find(|value| value.get("name").and_then(Value::as_str) == Some(binding))
        .and_then(|value| value.get("thread").and_then(Value::as_str))
        .ok_or(ApiError(
            StatusCode::NOT_FOUND,
            "delivery binding not found",
        ))?;
    let value = app
        .store
        .request_create(
            thread,
            &name,
            request_key,
            delivery_id,
            &payload,
            expires_at,
        )
        .map_err(storage_error)?;
    let value = request_view(value).map_err(storage_error)?;
    Ok((StatusCode::CREATED, Json(value)))
}

async fn request_list_handler(
    State(app): State<App>,
    uri: Uri,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let name = source(&app, &h)?;
    let values = query_values(&uri, &["thread", "limit", "after"])?;
    let thread = values
        .get("thread")
        .filter(|value| !value.is_empty())
        .ok_or(ApiError(
            StatusCode::BAD_REQUEST,
            "thread query parameter is required",
        ))?;
    let limit = values
        .get("limit")
        .map(|value| value.parse::<usize>())
        .transpose()
        .map_err(|_| {
            ApiError(
                StatusCode::BAD_REQUEST,
                "limit query parameter must be an integer",
            )
        })?
        .unwrap_or(100);
    if !(1..=100).contains(&limit) {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "limit query parameter must be between 1 and 100",
        ));
    }
    if let Some(after) = values.get("after")
        && after
            .parse::<i64>()
            .ok()
            .filter(|value| *value >= 0)
            .is_none()
    {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "request cursor must be a nonnegative integer",
        ));
    }
    Ok(Json(
        request_list(
            &app.store,
            thread,
            &name,
            limit,
            values.get("after").map(String::as_str),
        )
        .map_err(storage_error)?,
    ))
}

async fn request_get(
    State(app): State<App>,
    uri: Uri,
    Path(request_id): Path<String>,
    h: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let name = source(&app, &h)?;
    query_values(&uri, &[])?;
    if !valid_request_id(&request_id) {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "request id must be a nonempty identifier",
        ));
    }
    let value = request_by_id(&app.store, &name, &request_id)
        .map_err(storage_error)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "request not found"))?;
    Ok(Json(request_view(value).map_err(storage_error)?))
}

async fn request_update(
    State(app): State<App>,
    uri: Uri,
    Path(request_id): Path<String>,
    h: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let name = source(&app, &h)?;
    query_values(&uri, &[])?;
    if !valid_request_id(&request_id) {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "request id must be a nonempty identifier",
        ));
    }
    let request = request_record_by_id(&app.store, &name, &request_id)
        .map_err(storage_error)?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "request not found"))?;
    let body = request_body(&body)?;
    ensure_body_fields(body, &["update_id", "state", "expected_revision", "detail"])?;
    let update_id = request_string(body, "update_id")?;
    let state = request_string(body, "state")?;
    let expected_revision = body
        .get("expected_revision")
        .and_then(Value::as_i64)
        .filter(|value| *value >= 0)
        .ok_or(ApiError(
            StatusCode::BAD_REQUEST,
            "expected revision must be a nonnegative integer",
        ))?;
    let detail = match body.get("detail") {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.as_str().ok_or(ApiError(
            StatusCode::BAD_REQUEST,
            "request detail must be a string",
        ))?),
    };
    let thread = request
        .get("thread")
        .and_then(Value::as_str)
        .context("request thread missing")
        .map_err(storage_error)?;
    let key = request
        .get("request_key")
        .and_then(Value::as_str)
        .context("request key missing")
        .map_err(storage_error)?;
    let value = app
        .store
        .request_update(
            thread,
            &name,
            key,
            update_id,
            state,
            Some(expected_revision),
            detail,
        )
        .map_err(storage_error)?;
    Ok(Json(request_view(value).map_err(storage_error)?))
}

pub async fn shutdown_signal() {
    #[cfg(unix)]
    {
        if let Ok(mut term) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}};
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
pub async fn serve(
    store: Store,
    cfg: Config,
    root: PathBuf,
    pool: SessionPool,
    wake: Arc<Notify>,
    stop: CancellationToken,
) -> Result<()> {
    let admin_token = token(&root.join("admin.token"))?;
    let mut sources: BTreeMap<String, String> = BTreeMap::new();
    for (name, s) in &cfg.sources {
        let t = token(&s.token_file)?;
        if name == "managed/file" || same(&t, &admin_token) || sources.values().any(|x| same(x, &t))
        {
            bail!("distinct source/admin credentials required");
        }
        sources.insert(name.clone(), t);
    }
    let app = App {
        store: store.clone(),
        sources: Arc::new(sources),
        admin: Arc::new(admin_token),
        wake: wake.clone(),
        slots: Arc::new(Semaphore::new(32)),
    };
    let router = Router::new()
        .route("/v1/events/{binding}", post(ingest))
        .route("/v1/status", get(status))
        .route("/v1/sessions", get(sessions))
        .route("/v1/deliveries/{id}", get(delivery))
        .route("/v1/replies", get(replies))
        .route("/v1/replies/{id}/ack", post(ack))
        .route(
            "/v1/requests",
            get(request_list_handler).post(request_create),
        )
        .route("/v1/requests/{id}", get(request_get))
        .route("/v1/requests/{id}/updates", post(request_update))
        .layer(DefaultBodyLimit::max(32768))
        .layer(middleware::from_fn_with_state(app.clone(), guard))
        .with_state(app);
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", cfg.port)).await?;
    let mut workers = tokio::task::JoinSet::new();
    workers.spawn(collector::run(store.clone(), wake.clone(), stop.clone()));
    workers.spawn(reconcile(store.clone(), pool.clone(), stop.clone()));
    workers.spawn(request_maintenance(
        store.clone(),
        wake.clone(),
        stop.clone(),
    ));
    workers.spawn(dispatch(store, pool.clone(), wake, stop.clone()));
    let server_stop = stop.clone();
    workers.spawn(async move { http_server(listener, router, server_stop).await });
    output(json!({"ready":true,"runtime":"rust","port":cfg.port,"model_polling":false}));
    let result = tokio::select! {_=shutdown_signal()=>Ok(()),r=workers.join_next()=>match r{Some(Ok(Ok(())))=>Err(anyhow::anyhow!("receiver worker stopped unexpectedly")),Some(Ok(Err(e)))=>Err(e),Some(Err(e))=>Err(e.into()),None=>Ok(())}};
    stop.cancel();
    let _ = tokio::time::timeout(Duration::from_secs(8), async {
        while workers.join_next().await.is_some() {}
    })
    .await;
    workers.abort_all();
    pool.close().await;
    result
}
// Bound sockets before creating HTTP tasks, including peers that never send headers.
async fn http_server(
    listener: tokio::net::TcpListener,
    router: Router,
    stop: CancellationToken,
) -> Result<()> {
    let slots = Arc::new(Semaphore::new(64));
    let mut connections = tokio::task::JoinSet::new();
    loop {
        let permit = tokio::select! {_=stop.cancelled()=>break,p=slots.clone().acquire_owned()=>p?};
        let accepted = tokio::select! {_=stop.cancelled()=>break,a=listener.accept()=>a};
        let (stream, _) = accepted?;
        let service = hyper_util::service::TowerToHyperService::new(router.clone());
        let cancel = stop.clone();
        connections.spawn(async move {
            let _permit=permit;
            let mut builder=hyper::server::conn::http1::Builder::new();
            builder.timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(5)).max_buf_size(65536);
            let connection=builder.serve_connection(hyper_util::rt::TokioIo::new(stream),service);
            tokio::pin!(connection);
            tokio::select!{_=cancel.cancelled()=>{connection.as_mut().graceful_shutdown();let _=tokio::time::timeout(Duration::from_secs(5),connection).await;},_=tokio::time::timeout(Duration::from_secs(60),&mut connection)=>{}}
        });
        while let Some(result) = connections.try_join_next() {
            result?;
        }
    }
    while let Some(result) = connections.join_next().await {
        result?;
    }
    Ok(())
}
// This timer only examines local durable state. It never invokes a model or
// creates an event for an unchanged request.
async fn request_maintenance(
    store: Store,
    wake: Arc<Notify>,
    stop: CancellationToken,
) -> Result<()> {
    let mut tick = tokio::time::interval(Duration::from_millis(500));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = stop.cancelled() => return Ok(()),
            _ = tick.tick() => {}
        }
        let batch_store = store.clone();
        let processed =
            tokio::task::spawn_blocking(move || batch_store.request_maintenance(32)).await??;
        if processed > 0 {
            wake.notify_one();
        }
    }
}

async fn dispatch(
    store: Store,
    pool: SessionPool,
    wake: Arc<Notify>,
    stop: CancellationToken,
) -> Result<()> {
    let mut jobs = tokio::task::JoinSet::new();
    loop {
        while jobs.len() < 4 {
            let Some(e) = store.claim()? else { break };
            let s = store.clone();
            let p = pool.clone();
            jobs.spawn(async move { deliver_one(&s, &p, e).await });
        }
        tokio::select! {
            _=stop.cancelled()=>break,
            r=jobs.join_next(),if !jobs.is_empty()=>{if let Some(r)=r{r??;}},
            _=wake.notified()=>{},
            _=tokio::time::sleep(Duration::from_millis(500))=>{},

        }
    }
    jobs.abort_all();
    while jobs.join_next().await.is_some() {}
    Ok(())
}
async fn reconcile(store: Store, pool: SessionPool, stop: CancellationToken) -> Result<()> {
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    loop {
        tokio::select! {_=stop.cancelled()=>return Ok(()),_=tick.tick()=>{}}
        let rows = store.uncertain_events()?;
        let mut jobs = tokio::task::JoinSet::new();
        for batch in rows.as_array().context("uncertain event array")?.chunks(4) {
            for e in batch {
                let e = e.clone();
                let p = pool.clone();
                let s = store.clone();
                jobs.spawn(async move {
                    if let Ok(Ok(v)) = tokio::time::timeout(
                        Duration::from_secs(20),
                        p.inspect(
                            text(&e, "endpoint")?,
                            text(&e, "thread")?,
                            text(&e, "client_id")?,
                        ),
                    )
                    .await
                        && (v["state"] == "queued" || v["state"] == "consumed")
                    {
                        s.finish(
                            text(&e, "id")?,
                            "accepted",
                            v.get("submission_id").and_then(Value::as_str),
                            None,
                        )?;
                    }
                    Ok::<(), anyhow::Error>(())
                });
            }
            while !jobs.is_empty() {
                tokio::select! {_=stop.cancelled()=>{jobs.abort_all();return Ok(())},r=jobs.join_next()=>{if let Some(r)=r{r??;}}}
            }
        }
    }
}
async fn deliver_one(store: &Store, pool: &SessionPool, e: Value) -> Result<()> {
    let id = text(&e, "id")?;
    let endpoint = text(&e, "endpoint")?;
    let thread = text(&e, "thread")?;
    let envelope = e.get("envelope").context("envelope missing")?;
    let owned;
    let envelope = if let Some(s) = envelope.as_str() {
        owned = serde_json::from_str::<Value>(s)?;
        &owned
    } else {
        envelope
    };
    let rendered = render_event(text(&e, "binding")?, envelope, id);
    match pool
        .deliver(endpoint, thread, text(&e, "client_id")?, &rendered)
        .await
    {
        Ok(sub) => store.finish(id, "accepted", Some(&sub), None)?,
        Err(err) => {
            let kind = codex_monitor_rs::session::error_kind(&err);
            match kind {
                "unavailable" => {
                    store.defer_unavailable(id, "owner unavailable before submission")?
                }
                "permanent" => {
                    store.finish(id, "dead", None, Some("native submission rejected"))?
                }
                "retryable" => store.finish(
                    id,
                    "pending",
                    None,
                    Some("native submission temporarily rejected"),
                )?,
                _ => store.finish(
                    id,
                    "uncertain",
                    None,
                    Some("submission acceptance unknown; inspect before replay"),
                )?,
            }
        }
    }
    Ok(())
}
pub async fn resident(pool: SessionPool, endpoint: &str, threads: &[String]) -> Result<()> {
    codex_monitor_rs::session::validate_endpoint(endpoint)?;
    if endpoint == "shared-local" {
        bail!("resident requires the exact existing owner, not an independent shared-local writer");
    }
    let mut previous = Value::Null;
    let mut interval = tokio::time::interval(Duration::from_secs(2));
    loop {
        tokio::select! {_=shutdown_signal()=>break,_=interval.tick()=>{
            let mut states=Vec::new();for thread in threads {
                let result=pool.ensure_subscribed(endpoint,thread).await;
                states.push(json!({"thread":thread,"subscribed":result.is_ok(),"error":result.err().map(|_|"owner unavailable, unsupported, or conflicting") }));
            }
            let current=json!({"endpoint":endpoint,"targets":states,"model_polling":false,"approvals":"native client required"});if current!=previous{output(current.clone());previous=current;}
        }}
    }
    pool.close().await;
    Ok(())
}
