//! Event driven managed-file collector.
//!
//! The collector deliberately has a small responsibility boundary.  It owns
//! watches, scheduling and debounce state; `Store` owns the durable
//! checkpoint/event transaction.  File reads happen in a reusable, capped
//! pool of child processes so a hostile file (FIFO, device, or a filesystem
//! stuck in I/O) cannot pin the service task.

use anyhow::{Context, Result, anyhow};
use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, BufReader as AsyncBufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{Mutex, Notify, Semaphore, mpsc},
    time::{self, timeout},
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::store::Store;

pub const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
pub const READ_CHUNK: usize = 64 * 1024;
pub const MAX_WORKERS: usize = 4;
pub const SAMPLE_TIMEOUT: Duration = Duration::from_secs(2);
const RELOAD_INTERVAL: Duration = Duration::from_secs(2);
const MAX_WORKER_LINE: usize = 16 * 1024;
const MAX_CONDITION_BYTES: usize = 8 * 1024;
const MAX_POINTER_BYTES: usize = 1024;
const MAX_POINTER_DEPTH: usize = 32;
const MAX_EXPECTED_BYTES: usize = 4096;

static SAMPLE_COUNT: AtomicU64 = AtomicU64::new(0);
static SAMPLE_ERRORS: AtomicU64 = AtomicU64::new(0);
static EVENT_COUNT: AtomicU64 = AtomicU64::new(0);
static DIRTY_COUNT: AtomicU64 = AtomicU64::new(0);
static ACTIVE_WORKERS: AtomicUsize = AtomicUsize::new(0);

/// A deliberately small operational snapshot.  It is process-wide because
/// `run` is normally owned by the service and has no public collector handle.
pub fn stats() -> Value {
    json!({
        "samples": SAMPLE_COUNT.load(Ordering::Relaxed),
        "sample_errors": SAMPLE_ERRORS.load(Ordering::Relaxed),
        "events": EVENT_COUNT.load(Ordering::Relaxed),
        "dirty": DIRTY_COUNT.load(Ordering::Relaxed),
        "active_workers": ACTIVE_WORKERS.load(Ordering::Relaxed),
        "worker_limit": MAX_WORKERS,
    })
}

#[derive(Clone, Debug)]
struct Watch {
    id: String,
    name: String,
    path: PathBuf,
    interval: Duration,
    debounce: Duration,
    condition: Option<Value>,
    enabled: bool,
    removed: bool,
    epoch: i64,
}

#[derive(Clone, Debug)]
struct Candidate {
    sample: Value,
    first_seen: Instant,
    epoch: i64,
}

#[derive(Clone, Default)]
struct Runtime {
    watch: Option<Watch>,
    candidate: Option<Candidate>,
    next_due: Option<Instant>,
    dirty: bool,
    native_path: Option<PathBuf>,
}

fn watch_from_value(value: &Value) -> Result<Watch> {
    let object = value.as_object().context("watch must be an object")?;
    let text = |key: &str| -> Result<String> {
        object
            .get(key)
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .with_context(|| format!("watch.{key} must be a string"))
    };
    let number = |key: &str| -> Result<f64> {
        object
            .get(key)
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .with_context(|| format!("watch.{key} must be finite and non-negative"))
    };
    let enabled = object
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let removed = object
        .get("removed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let epoch = object.get("epoch").and_then(Value::as_i64).unwrap_or(0);
    let path = PathBuf::from(text("path")?);
    let interval = Duration::from_secs_f64(number("interval")?.max(0.1));
    let debounce = Duration::from_secs_f64(number("debounce")?.min(86_400.0));
    let condition = object.get("condition").filter(|v| !v.is_null()).cloned();
    if condition
        .as_ref()
        .and_then(|value| serde_json::to_vec(value).ok())
        .is_some_and(|encoded| encoded.len() > MAX_CONDITION_BYTES)
    {
        return Err(anyhow!("watch.condition exceeds bounded size"));
    }
    Ok(Watch {
        id: text("id")?,
        name: object
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        path,
        interval,
        debounce,
        condition,
        enabled,
        removed,
        epoch,
    })
}

fn absolute_display_path(path: &Path) -> String {
    if path.is_absolute() {
        return path.to_string_lossy().into_owned();
    }
    env::current_dir()
        .map(|cwd| cwd.join(path))
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .into_owned()
}

#[cfg(unix)]
fn sample_file(path: &Path, condition: Option<&Value>) -> Value {
    use std::{
        fs,
        os::fd::AsRawFd,
        os::unix::fs::{MetadataExt, OpenOptionsExt},
    };
    let display = absolute_display_path(path);
    let lstat = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return json!({"path": display, "state": "missing"});
        }
        Err(error) => return unreadable(&display, error_name(&error)),
    };
    if lstat.file_type().is_symlink() {
        return unreadable(&display, "symlink");
    }
    if !lstat.file_type().is_file() {
        return unreadable(&display, "not_regular_file");
    }
    if lstat.len() > MAX_FILE_BYTES {
        return unreadable(&display, "file_too_large");
    }
    let mut options = fs::OpenOptions::new();
    options
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC);
    let mut file = match options.open(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return json!({"path": display, "state": "missing"});
        }
        Err(error) => return unreadable(&display, error_name(&error)),
    };
    let before = match file.metadata() {
        Ok(value) => value,
        Err(error) => return unreadable(&display, error_name(&error)),
    };
    if !before.file_type().is_file() {
        return unreadable(&display, "not_regular_file");
    }
    if before.len() > MAX_FILE_BYTES {
        return unreadable(&display, "file_too_large");
    }
    let mut digest = Sha256::new();
    let mut content = if condition.is_some() {
        Some(Vec::with_capacity(before.len() as usize))
    } else {
        None
    };
    let mut remaining = before.len();
    let deadline = Instant::now() + Duration::from_millis(250);
    let mut block = vec![0_u8; READ_CHUNK];
    while remaining > 0 {
        if Instant::now() >= deadline {
            return unreadable(&display, "read_timeout");
        }
        let requested = std::cmp::min(remaining as usize, block.len());
        let read = match file.read(&mut block[..requested]) {
            Ok(0) => return unreadable(&display, "short_read"),
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                return unreadable(&display, "read_timeout");
            }
            Err(error) => return unreadable(&display, error_name(&error)),
        };
        digest.update(&block[..read]);
        if let Some(bytes) = content.as_mut() {
            bytes.extend_from_slice(&block[..read]);
        }
        remaining -= read as u64;
    }
    let after = match file.metadata() {
        Ok(value) => value,
        Err(error) => return unreadable(&display, error_name(&error)),
    };
    let identity = |m: &fs::Metadata| (m.dev(), m.ino(), m.len(), m.mtime_nsec());
    if identity(&before) != identity(&after) {
        return unreadable(&display, "changed_during_read");
    }
    let mut result =
        json!({"path": display, "state": "present", "sha256": format!("{:x}", digest.finalize())});
    if let Some(condition) = condition {
        match content
            .as_deref()
            .and_then(|bytes| serde_json::from_slice::<Value>(bytes).ok())
        {
            Some(document) => match evaluate_condition(&document, condition) {
                Ok(predicate) => result["predicate"] = predicate,
                Err(_) => {
                    result["predicate"] = json!({"state":"invalid","error":"invalid_condition"})
                }
            },
            None => return unreadable(&display, "invalid_json"),
        }
    }
    let _ = file.as_raw_fd(); // keep the descriptor explicitly owned until after fstat.
    result
}

/// Sample a path using the same bounded reader used by collector workers.
/// Exposed for contract tests and for callers that need a one-shot diagnostic.
pub fn safe_file_sample(path: &Path) -> Value {
    sample_file(path, None)
}

/// Sample a JSON path and return only the bounded predicate result.
pub fn safe_json_sample(path: &Path, condition: &Value) -> Value {
    sample_file(path, Some(condition))
}

#[cfg(not(unix))]
fn sample_file(path: &Path, condition: Option<&Value>) -> Value {
    let display = absolute_display_path(path);
    let bytes = match std::fs::read(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return json!({"path": display, "state":"missing"});
        }
        Err(error) => return unreadable(&display, error_name(&error)),
    };
    if bytes.len() as u64 > MAX_FILE_BYTES {
        return unreadable(&display, "file_too_large");
    }
    let mut hash = Sha256::new();
    hash.update(&bytes);
    let mut result =
        json!({"path":display,"state":"present","sha256":format!("{:x}", hash.finalize())});
    if let Some(condition) = condition {
        match serde_json::from_slice::<Value>(&bytes)
            .ok()
            .and_then(|v| evaluate_condition(&v, condition).ok())
        {
            Some(predicate) => result["predicate"] = predicate,
            None => result["predicate"] = json!({"state":"invalid","error":"invalid_condition"}),
        }
    }
    result
}

fn error_name(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => "PermissionDenied",
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::InvalidInput => "InvalidInput",
        _ => "OSError",
    }
}

fn unreadable(path: &str, error: &str) -> Value {
    json!({"path":path,"state":"unreadable","error":error})
}

fn pointer_tokens(pointer: &str) -> Result<Vec<String>> {
    if pointer.len() > MAX_POINTER_BYTES || (!pointer.is_empty() && !pointer.starts_with('/')) {
        return Err(anyhow!("invalid JSON pointer"));
    }
    if pointer.is_empty() {
        return Ok(Vec::new());
    }
    let mut result = Vec::new();
    for part in pointer[1..].split('/') {
        if result.len() >= MAX_POINTER_DEPTH {
            return Err(anyhow!("JSON pointer too deep"));
        }
        let mut token = String::new();
        let bytes = part.as_bytes();
        let mut i = 0;
        while i < bytes.len() {
            if bytes[i] != b'~' {
                token.push(part[i..].chars().next().unwrap());
                i += part[i..].chars().next().unwrap().len_utf8();
                continue;
            }
            if i + 1 >= bytes.len() || (bytes[i + 1] != b'0' && bytes[i + 1] != b'1') {
                return Err(anyhow!("invalid JSON pointer escape"));
            }
            token.push(if bytes[i + 1] == b'0' { '~' } else { '/' });
            i += 2;
        }
        result.push(token);
    }
    Ok(result)
}

fn strict_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null)
        | (Value::Bool(_), Value::Bool(_))
        | (Value::String(_), Value::String(_)) => left == right,
        (Value::Number(a), Value::Number(b)) => a.as_f64() == b.as_f64(),
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| strict_equal(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, v)| b.get(k).is_some_and(|other| strict_equal(v, other)))
        }
        _ => false,
    }
}

fn evaluate_condition(document: &Value, condition: &Value) -> Result<Value> {
    let object = condition
        .as_object()
        .context("condition must be an object")?;
    let pointer = object
        .get("pointer")
        .and_then(Value::as_str)
        .context("pointer")?;
    let operator = object
        .get("operator")
        .and_then(Value::as_str)
        .context("operator")?;
    if !matches!(operator, "eq" | "ne" | "gt" | "gte" | "lt" | "lte") {
        return Err(anyhow!("invalid operator"));
    }
    let expected = object.get("value").context("value")?;
    if serde_json::to_vec(expected)?.len() > MAX_EXPECTED_BYTES {
        return Err(anyhow!("expected value too large"));
    }
    let tokens = pointer_tokens(pointer)?;
    let mut current = document;
    for token in tokens {
        current = match current {
            Value::Object(map) => map.get(&token).context("missing_pointer")?,
            Value::Array(items) => {
                if token.is_empty()
                    || (token.len() > 1 && token.starts_with('0'))
                    || !token.bytes().all(|b| b.is_ascii_digit())
                {
                    return Err(anyhow!("missing_pointer"));
                }
                items
                    .get(token.parse::<usize>()?)
                    .context("missing_pointer")?
            }
            _ => return Err(anyhow!("missing_pointer")),
        };
    }
    let ordering_number = |value: &Value| -> Option<f64> {
        if value.is_boolean() || !value.is_number() {
            None
        } else {
            value.as_f64().filter(|v| v.is_finite())
        }
    };
    if matches!(operator, "gt" | "gte" | "lt" | "lte") {
        let left = ordering_number(current).context("ordering_requires_number")?;
        let right = ordering_number(expected).context("ordering_requires_number")?;
        let matched = match operator {
            "gt" => left > right,
            "gte" => left >= right,
            "lt" => left < right,
            _ => left <= right,
        };
        return Ok(json!({"state": if matched {"matched"} else {"not_matched"}}));
    }
    let matched = if operator == "eq" {
        strict_equal(current, expected)
    } else {
        !strict_equal(current, expected)
    };
    Ok(json!({"state": if matched {"matched"} else {"not_matched"}}))
}

#[derive(serde::Serialize, serde::Deserialize)]
struct WorkerJob {
    path: String,
    #[serde(default)]
    condition: Option<Value>,
}
#[derive(serde::Serialize, serde::Deserialize)]
struct WorkerReply {
    ok: bool,
    sample: Option<Value>,
    error: Option<String>,
}

/// JSON-lines entry point used by the hidden `__sample-worker` command.
pub async fn worker_main() -> Result<()> {
    worker_main_sync()
}

/// Minimal worker entry point: no async runtime or reactor threads are needed.
pub fn worker_main_sync() -> Result<()> {
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    loop {
        let line = match read_sync_line(&mut input)? {
            Some(line) => line,
            None => break,
        };
        if line.len() > MAX_WORKER_LINE {
            serde_json::to_writer(
                &mut output,
                &WorkerReply {
                    ok: false,
                    sample: None,
                    error: Some("worker_request".into()),
                },
            )?;
            output.write_all(b"\n")?;
            continue;
        }
        let reply = match serde_json::from_slice::<WorkerJob>(&line) {
            Ok(job) => {
                let path = Path::new(&job.path);
                let mut sample = sample_file(path, job.condition.as_ref());
                // Resolve directory aliases only inside the killable worker.
                // This internal key is removed before checkpoint/event storage.
                if let (Some(parent), Some(name)) = (path.parent(), path.file_name())
                    && let Ok(resolved) = std::fs::canonicalize(parent)
                {
                    sample["_native_path"] = json!(resolved.join(name));
                }
                WorkerReply {
                    ok: true,
                    sample: Some(sample),
                    error: None,
                }
            }
            Err(error) => WorkerReply {
                ok: false,
                sample: None,
                error: Some(format!("worker_request:{error}")),
            },
        };
        serde_json::to_writer(&mut output, &reply)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}

fn read_sync_line<R: Read>(input: &mut R) -> Result<Option<Vec<u8>>> {
    let mut line = Vec::with_capacity(MAX_WORKER_LINE.min(1024));
    let mut byte = [0_u8; 1];
    loop {
        let read = input.read(&mut byte)?;
        if read == 0 {
            return if line.is_empty() {
                Ok(None)
            } else {
                Ok(Some(line))
            };
        }
        line.push(byte[0]);
        if byte[0] == b'\n' {
            return Ok(Some(line));
        }
        if line.len() > MAX_WORKER_LINE {
            // Drain through the record terminator before accepting another
            // request, while keeping the oversized record bounded in memory.
            loop {
                let read = input.read(&mut byte)?;
                if read == 0 || byte[0] == b'\n' {
                    break;
                }
            }
            return Ok(Some(Vec::new()));
        }
    }
}

struct WorkerConnection {
    child: Child,
    stdin: ChildStdin,
    stdout: AsyncBufReader<ChildStdout>,
}

impl WorkerConnection {
    async fn start() -> Result<Self> {
        let executable = env::var_os("CODEX_MONITOR_SAMPLE_WORKER")
            .unwrap_or(env::current_exe()?.into_os_string());
        let mut child = Command::new(executable)
            .arg("__sample-worker")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .context("start sample worker")?;
        let stdin = child.stdin.take().context("sample worker stdin")?;
        let stdout = child.stdout.take().context("sample worker stdout")?;
        Ok(Self {
            child,
            stdin,
            stdout: AsyncBufReader::new(stdout),
        })
    }

    async fn sample(&mut self, path: &Path, condition: Option<&Value>) -> Result<Value> {
        let request = serde_json::to_vec(&WorkerJob {
            path: absolute_display_path(path),
            condition: condition.cloned(),
        })?;
        if request.len() > MAX_WORKER_LINE {
            return Err(anyhow!("worker_request"));
        }
        self.stdin.write_all(&request).await?;
        self.stdin.write_all(b"\n").await?;
        self.stdin.flush().await?;
        let line = read_async_line(&mut self.stdout).await?;
        let reply: WorkerReply = serde_json::from_slice(&line).context("worker_result")?;
        if !reply.ok {
            return Err(anyhow!(
                reply.error.unwrap_or_else(|| "worker_result".into())
            ));
        }
        reply.sample.context("worker_result")
    }

    async fn kill(&mut self) {
        let _ = self.child.kill().await;
        let _ = self.child.wait().await;
    }
}

async fn read_async_line(reader: &mut AsyncBufReader<ChildStdout>) -> Result<Vec<u8>> {
    let mut line = Vec::with_capacity(MAX_WORKER_LINE.min(1024));
    let mut byte = [0_u8; 1];
    loop {
        let read = reader.read(&mut byte).await?;
        if read == 0 {
            return if line.is_empty() {
                Err(anyhow!("worker_exited"))
            } else {
                Err(anyhow!("worker_result"))
            };
        }
        line.push(byte[0]);
        if byte[0] == b'\n' {
            return Ok(line);
        }
        if line.len() > MAX_WORKER_LINE {
            return Err(anyhow!("worker_result"));
        }
    }
}

struct WorkerPool {
    idle: Mutex<Vec<WorkerConnection>>,
    gate: Semaphore,
}
impl WorkerPool {
    fn new() -> Self {
        Self {
            idle: Mutex::new(Vec::new()),
            gate: Semaphore::new(MAX_WORKERS),
        }
    }
    async fn sample(&self, path: &Path, condition: Option<&Value>) -> Result<Value> {
        let _permit = self.gate.acquire().await?;
        let idle = self.idle.lock().await.pop();
        let mut worker = if let Some(worker) = idle {
            worker
        } else {
            let worker = WorkerConnection::start().await?;
            ACTIVE_WORKERS.fetch_add(1, Ordering::Relaxed);
            worker
        };
        let result = timeout(SAMPLE_TIMEOUT, worker.sample(path, condition)).await;
        match result {
            Ok(Ok(value)) => {
                self.idle.lock().await.push(worker);
                Ok(value)
            }
            error => {
                worker.kill().await;
                ACTIVE_WORKERS.fetch_sub(1, Ordering::Relaxed);
                match error {
                    Ok(Err(e)) => Err(e),
                    _ => Err(anyhow!("worker_timeout")),
                }
            }
        }
    }
    async fn close(&self) {
        for mut worker in self.idle.lock().await.drain(..) {
            worker.kill().await;
            ACTIVE_WORKERS.fetch_sub(1, Ordering::Relaxed);
        }
    }
}

const EVENT_QUEUE_CAPACITY: usize = 1024;

fn notify_path(
    event: Result<Event, notify::Error>,
    sender: &mpsc::Sender<PathBuf>,
    overflow: &AtomicBool,
) {
    if let Ok(event) = event {
        if !matches!(
            event.kind,
            EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_)
        ) {
            return;
        }
        for path in event.paths {
            if sender.try_send(path).is_err() {
                // Losing a native event is safe: the fallback scan will make
                // every active target dirty on the next dispatcher turn.
                overflow.store(true, Ordering::Release);
            }
            DIRTY_COUNT.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn parent_for(path: &Path) -> PathBuf {
    path.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf()
}

fn make_event(watch: &Watch, previous: &Value, current: &Value) -> Value {
    json!({"id":Uuid::new_v4().to_string(),"source":"managed/file","type":"monitor.file.changed","hops":0,"data":{"watch":watch.name,"path":absolute_display_path(&watch.path),"previous":previous,"current":current}})
}

fn changed(previous: &Value, current: &Value) -> bool {
    previous != current
}

async fn process_watch(
    store: &Store,
    runtime: &mut Runtime,
    worker_pool: &WorkerPool,
    wakeup: &Arc<Notify>,
    now: Instant,
) -> Result<()> {
    let watch = match runtime.watch.clone() {
        Some(value) if value.enabled && !value.removed => value,
        _ => return Ok(()),
    };
    let mut sample = worker_pool
        .sample(&watch.path, watch.condition.as_ref())
        .await?;
    runtime.native_path = sample
        .as_object_mut()
        .and_then(|sample| sample.remove("_native_path"))
        .and_then(|path| path.as_str().map(PathBuf::from));
    SAMPLE_COUNT.fetch_add(1, Ordering::Relaxed);
    if sample.get("state").and_then(Value::as_str) == Some("unreadable")
        && matches!(
            sample.get("error").and_then(Value::as_str),
            Some(
                "read_timeout"
                    | "changed_during_read"
                    | "short_read"
                    | "worker_timeout"
                    | "worker_exited"
            )
        )
    {
        SAMPLE_ERRORS.fetch_add(1, Ordering::Relaxed);
        return Ok(());
    }
    if watch.condition.is_some() && sample.get("state").and_then(Value::as_str) != Some("present") {
        SAMPLE_ERRORS.fetch_add(1, Ordering::Relaxed);
        return Ok(());
    }
    let mut observed = sample.clone();
    if watch.condition.is_some() {
        let state = sample
            .get("predicate")
            .and_then(|v| v.get("state"))
            .and_then(Value::as_str)
            .context("invalid condition result")?;
        if !matches!(state, "matched" | "not_matched") {
            return Err(anyhow!("invalid condition result"));
        }
        observed = json!({"condition":state});
    }
    // A read can outlive pause/remove. `commit_sample` performs the atomic
    // epoch/enabled check, so avoid a full watch-table reload for every file.
    let baseline = store.checkpoint(&watch.id)?;
    let Some(baseline) = baseline else {
        store.commit_sample(&watch.id, watch.epoch, &observed, None)?;
        runtime.candidate = None;
        runtime.next_due = Some(now + watch.interval);
        return Ok(());
    };
    if !changed(&baseline, &observed) {
        runtime.candidate = None;
        runtime.next_due = Some(now + watch.interval);
        return Ok(());
    }
    if watch.debounce.is_zero() {
        let event = make_event(&watch, &baseline, &observed);
        let receipt = store.commit_sample(&watch.id, watch.epoch, &observed, Some(&event))?;
        if receipt.is_some() {
            EVENT_COUNT.fetch_add(1, Ordering::Relaxed);
            wakeup.notify_one();
        }
        runtime.candidate = None;
    } else {
        let reset = runtime
            .candidate
            .as_ref()
            .is_none_or(|candidate| candidate.sample != observed || candidate.epoch != watch.epoch);
        if reset {
            runtime.candidate = Some(Candidate {
                sample: observed,
                first_seen: now,
                epoch: watch.epoch,
            });
        } else if now.duration_since(runtime.candidate.as_ref().expect("candidate").first_seen)
            >= watch.debounce
        {
            let candidate = runtime.candidate.take().expect("candidate").sample;
            let event = make_event(&watch, &baseline, &candidate);
            let receipt = store.commit_sample(&watch.id, watch.epoch, &candidate, Some(&event))?;
            if receipt.is_some() {
                EVENT_COUNT.fetch_add(1, Ordering::Relaxed);
                wakeup.notify_one();
            }
        }
    }
    runtime.next_due = Some(now + watch.interval);
    Ok(())
}

/// Run the event-driven collector until `stop` is cancelled.
pub async fn run(store: Store, wakeup: Arc<Notify>, stop: CancellationToken) -> Result<()> {
    let (event_sender, mut event_receiver) = mpsc::channel::<PathBuf>(EVENT_QUEUE_CAPACITY);
    let event_overflow = Arc::new(AtomicBool::new(false));
    let overflow_for_callback = Arc::clone(&event_overflow);
    let mut native = RecommendedWatcher::new(
        move |event| notify_path(event, &event_sender, &overflow_for_callback),
        Config::default(),
    )
    .context("create file watcher")?;
    let mut runtimes: HashMap<String, Runtime> = HashMap::new();
    let mut registered: HashMap<PathBuf, usize> = HashMap::new();
    let worker_pool = Arc::new(WorkerPool::new());
    let mut reload = time::interval(RELOAD_INTERVAL);
    reload.set_missed_tick_behavior(time::MissedTickBehavior::Delay);
    let mut fair_cursor = 0usize;
    let mut jobs = tokio::task::JoinSet::<(String, Runtime)>::new();
    let mut in_flight = HashSet::new();
    // Keep cleanup outside the fallible dispatcher body.  In particular, a
    // transient store error must still reap every persistent child.
    let result: Result<()> = async {
        loop {
        let next_due=runtimes.iter().filter(|(id,r)|!in_flight.contains(*id)&&r.watch.as_ref().is_some_and(|w|w.enabled&&!w.removed)).filter_map(|(_,r)|if r.dirty{Some(Instant::now())}else{r.next_due}).min().unwrap_or_else(||Instant::now()+Duration::from_secs(3600));
        tokio::select! {
            _ = stop.cancelled() => break Ok(()),
            result=jobs.join_next(),if !jobs.is_empty()=>{
                if let Some(result)=result {
                    let (id,mut finished)=result?;in_flight.remove(&id);
                    if let Some(current)=runtimes.get_mut(&id) && current.watch.as_ref().map(|w|w.epoch)==finished.watch.as_ref().map(|w|w.epoch) {
                        finished.dirty|=current.dirty;*current=finished;
                    }
                }
            },
            _ = reload.tick() => {
                let rows = store.watches(None)?.as_array().cloned().unwrap_or_default();
                let mut current = HashSet::new();
                for value in rows {
                    let watch = match watch_from_value(&value) { Ok(value) => value, Err(_) => continue };
                    current.insert(watch.id.clone());
                    let runtime = runtimes.entry(watch.id.clone()).or_default();
                    let old = runtime.watch.clone();
                    let epoch_changed = old.as_ref().is_some_and(|prior| prior.epoch != watch.epoch);
                    let path_changed = old.as_ref().is_none_or(|prior| prior.path != watch.path);
                    let was_active = old.as_ref().is_some_and(|prior| prior.enabled && !prior.removed);
                    let is_active = watch.enabled && !watch.removed;
                    if (path_changed || was_active != is_active) && let Some(old) = runtime.watch.as_ref() {
                            let parent = parent_for(&old.path);
                            if let Some(count) = registered.get_mut(&parent) {
                                *count = count.saturating_sub(1);
                                if *count == 0 {
                                    let _ = native.unwatch(&parent);
                                    registered.remove(&parent);
                                }
                            }
                    }
                    if is_active {
                        let parent = parent_for(&watch.path);
                        if path_changed || !was_active {
                            if !registered.contains_key(&parent) && native.watch(&parent, RecursiveMode::NonRecursive).is_ok() {
                                registered.insert(parent.clone(), 1);
                            } else if let Some(count) = registered.get_mut(&parent) {
                                *count += 1;
                            }
                        }
                        if path_changed || !was_active || epoch_changed { runtime.dirty = true; }
                        if runtime.next_due.is_none() || epoch_changed { runtime.next_due = Some(Instant::now()); }
                    }
                    if epoch_changed { runtime.candidate = None; }
                    runtime.watch = Some(watch);
                }
                for id in runtimes.keys().cloned().collect::<Vec<_>>() {
                    if !current.contains(&id) {
                        if let Some(runtime) = runtimes.get(&id) && let Some(watch) = runtime.watch.as_ref().filter(|watch| watch.enabled && !watch.removed) {
                                let parent = parent_for(&watch.path);
                                if let Some(count) = registered.get_mut(&parent) {
                                    *count = count.saturating_sub(1);
                                    if *count == 0 { let _ = native.unwatch(&parent); registered.remove(&parent); }
                                }
                        }
                        runtimes.remove(&id);
                    }
                }
            },
            Some(path) = event_receiver.recv() => {
                for runtime in runtimes.values_mut() {
                    if runtime.watch.as_ref().is_some_and(|watch| {
                        watch.path==path || parent_for(&watch.path)==path || runtime.native_path.as_ref().is_some_and(|alias|*alias==path||parent_for(alias)==path)
                    }) {
                        runtime.dirty = true;
                    }
                }
            },
            _ = time::sleep_until(next_due.into()),if jobs.len()<MAX_WORKERS => {},
        }
        if event_overflow.swap(false, Ordering::AcqRel) {
            for runtime in runtimes.values_mut() {
                if runtime.watch.as_ref().is_some_and(|watch| watch.enabled && !watch.removed) {
                    runtime.dirty = true;
                }
            }
        }
        let now = Instant::now();
        let ids: Vec<String> = runtimes.keys().cloned().collect();
        if !ids.is_empty() {
            let start = fair_cursor % ids.len();
            fair_cursor = (start + 1) % ids.len();

            for offset in 0..ids.len() {
                if jobs.len() >= MAX_WORKERS {
                    break;
                }
                let id = ids[(start + offset) % ids.len()].clone();
                let due = !in_flight.contains(&id) && runtimes.get(&id).is_some_and(|runtime| {
                    runtime.watch.as_ref().is_some_and(|w|w.enabled&&!w.removed) && (runtime.dirty || runtime.next_due.is_some_and(|at| now >= at))
                });
                if !due {
                    continue;
                }
                let Some(current) = runtimes.get_mut(&id) else {
                    continue;
                };
                current.dirty=false;
                let mut runtime=current.clone();
                in_flight.insert(id.clone());
                let worker_pool = Arc::clone(&worker_pool);
                let store = store.clone();
                let wakeup = Arc::clone(&wakeup);
                jobs.spawn(async move {
                    let result =
                        process_watch(&store, &mut runtime, &worker_pool, &wakeup, now).await;
                    if result.is_err() {
                        SAMPLE_ERRORS.fetch_add(1, Ordering::Relaxed);
                        runtime.next_due = Some(
                            now + runtime
                                .watch
                                .as_ref()
                                .map(|w| w.interval)
                                .unwrap_or(Duration::from_secs(1)),
                        );
                    }
                    (id, runtime)
                });
            }
        }
        }
    }
    .await;
    while jobs.join_next().await.is_some() {}
    worker_pool.close().await;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn predicate_pointer_and_strict_comparison_are_bounded() {
        let document = json!({"a/b":[{"n":3}]});
        let condition = json!({"pointer":"/a~1b/0/n","operator":"gte","value":3});
        assert_eq!(
            evaluate_condition(&document, &condition).unwrap(),
            json!({"state":"matched"})
        );
        assert!(pointer_tokens(&"/x".repeat(1025)).is_err());
    }
}
