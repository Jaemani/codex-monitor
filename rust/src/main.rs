mod runtime;
mod ui;
use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use codex_monitor_rs::{
    collector,
    session::{SessionPool, validate_endpoint},
    store::Store,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

#[derive(Parser)]
#[command(
    version,
    about = "Rust event monitoring runtime; isolated state, experimental Codex queue"
)]
struct Cli {
    #[arg(long, global = true, env = "CODEX_MONITOR_RUST_HOME")]
    state: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(long, default_value_t = 8876)]
        port: u16,
    },
    Source {
        name: String,
    },
    #[command(alias = "attach")]
    Bind {
        name: String,
        #[arg(long)]
        thread: String,
        #[arg(long, required = true)]
        source: Vec<String>,
        #[arg(long, default_value = "shared-local")]
        endpoint: String,
    },
    Conversation {
        #[command(subcommand)]
        action: Conversation,
    },
    Serve,
    Status,
    Sessions {
        name: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Dashboard {
        #[arg(long)]
        once: bool,
        #[arg(long)]
        json: bool,
        #[arg(long)]
        thread: Option<String>,
        #[arg(long, default_value_t = 2.0)]
        interval: f64,
        #[arg(long,default_value="auto",value_parser=["auto","always","never"])]
        color: String,
        #[arg(long)]
        no_animate: bool,
    },
    Monitor {
        #[command(subcommand)]
        action: Watch,
    },
    Event {
        id: String,
    },
    Inspect {
        id: String,
    },
    #[command(alias = "disable")]
    Pause {
        binding: String,
    },
    #[command(alias = "enable")]
    Unpause {
        binding: String,
    },
    Remove {
        binding: String,
    },
    Request {
        #[command(subcommand)]
        action: Request,
    },
    Reply {
        delivery_id: String,
        #[arg(long)]
        id: String,
        #[arg(long)]
        message: String,
    },
    Send {
        #[arg(long)]
        to: String,
        #[arg(long)]
        source: String,
        #[arg(long)]
        id: String,
        #[arg(long, default_value = "agent.message")]
        r#type: String,
        #[arg(long, default_value = "{}")]
        data: String,
        #[arg(long)]
        url: Option<String>,
        #[arg(long)]
        token_env: Option<String>,
        #[arg(long)]
        trace: Option<String>,
        #[arg(long, default_value_t = 0)]
        hops: u64,
    },
    Doctor {
        #[arg(long, default_value = "shared-local")]
        endpoint: String,
        #[arg(long)]
        thread: Option<String>,
        #[arg(long,default_value="cli",value_parser=["cli","desktop"])]
        surface: String,
        #[arg(long)]
        require_consumer: bool,
    },
    Host {
        #[arg(long, default_value_t = 8765)]
        port: u16,
    },
    Connect {
        #[arg(long)]
        endpoint: String,
        #[arg(long)]
        thread: Option<String>,
        #[arg(long)]
        cwd: Option<PathBuf>,
    },
    Resident {
        #[arg(long)]
        endpoint: String,
        #[arg(long, required = true)]
        thread: Vec<String>,
    },
    #[command(name = "__sample-worker", hide = true)]
    SampleWorker,
}
#[derive(Subcommand)]
enum Conversation {
    Set {
        #[arg(long)]
        thread: String,
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
    },
    List {
        #[arg(long)]
        thread: Option<String>,
    },
}
#[derive(Subcommand)]
enum Watch {
    Create {
        name: String,
        #[arg(long)]
        thread: String,
        #[arg(long)]
        file: PathBuf,
        #[arg(long, default_value_t = 2.0)]
        interval: f64,
        #[arg(long, default_value = "shared-local")]
        endpoint: String,
        #[arg(long, default_value_t = 0.0)]
        debounce: f64,
        #[arg(long)]
        json_pointer: Option<String>,
        #[arg(long,value_parser=["eq","ne","gt","gte","lt","lte"])]
        operator: Option<String>,
        #[arg(long)]
        value: Option<String>,
    },
    List {
        #[arg(long)]
        thread: Option<String>,
    },
    Status {
        name: String,
        #[arg(long)]
        thread: String,
    },
    Pause {
        name: String,
        #[arg(long)]
        thread: String,
    },
    Resume {
        name: String,
        #[arg(long)]
        thread: String,
    },
    Remove {
        name: String,
        #[arg(long)]
        thread: String,
    },
}
#[derive(Subcommand)]
enum Request {
    Create {
        #[arg(long)]
        thread: String,
        #[arg(long)]
        source: String,
        #[arg(long)]
        id: String,
        #[arg(long)]
        delivery: String,
        #[arg(long, default_value = "{}")]
        payload: String,
        #[arg(long)]
        expires_at: Option<f64>,
    },
    Update {
        #[arg(long)]
        thread: String,
        #[arg(long)]
        source: String,
        #[arg(long)]
        id: String,
        #[arg(long)]
        update_id: String,
        #[arg(long)]
        status: String,
        #[arg(long)]
        expected_revision: Option<i64>,
        #[arg(long)]
        detail: Option<String>,
    },
    List {
        #[arg(long)]
        thread: String,
        #[arg(long)]
        source: Option<String>,
    },
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Source {
    token_file: PathBuf,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Config {
    version: u32,
    runtime: String,
    port: u16,
    sources: BTreeMap<String, Source>,
}
pub fn output(v: Value) {
    println!("{}", serde_json::to_string_pretty(&v).expect("JSON value"));
}
pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .with_context(|| format!("missing {key}"))
}
pub fn private_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = path.with_extension(format!("tmp-{}", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut opts = OpenOptions::new();
        opts.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&tmp, path)?;
        File::open(path.parent().context("parent path")?)?.sync_all()?;
        Ok(())
    })();
    if tmp.exists() {
        let _ = fs::remove_file(tmp);
    }
    result
}
pub fn lock(root: &Path, name: &str) -> Result<File> {
    let mut opts = OpenOptions::new();
    opts.create(true).truncate(false).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let f = opts.open(root.join(name))?;
    fs2::FileExt::try_lock_exclusive(&f).context("another process holds the state lock")?;
    Ok(f)
}
pub fn token(path: &Path) -> Result<String> {
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let f = opts.open(path).context("cannot open credential file")?;
    let meta = f.metadata()?;
    if !meta.is_file() || meta.len() > 65536 {
        bail!("credential must be a bounded regular file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.mode() & 0o077 != 0 || meta.uid() != unsafe { libc::geteuid() } {
            bail!("credential file must be private and owned by the current user");
        }
    }
    let mut s = String::new();
    f.take(65537).read_to_string(&mut s)?;
    let s = s.trim();
    if s.is_empty() || !s.bytes().all(|c| (33..=126).contains(&c)) {
        bail!("invalid credential format");
    }
    Ok(s.into())
}
fn read_input(s: &str) -> Result<String> {
    if s != "-" {
        return Ok(s.into());
    }
    let mut v = String::new();
    std::io::stdin().take(32769).read_to_string(&mut v)?;
    if v.len() > 32768 {
        bail!("input exceeds 32 KiB");
    }
    Ok(v)
}
fn config(root: &Path) -> Result<Config> {
    let c: Config = serde_json::from_slice(
        &fs::read(root.join("config.json"))
            .context("run init on a separate Rust state directory first")?,
    )
    .context("not Rust state; automatic Python migration is unavailable")?;
    if c.runtime != "rust" || c.version != 1 || c.port == 0 {
        bail!("unsupported Rust configuration");
    }
    Ok(c)
}
fn valid_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 200
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.:@-".contains(&c))
}
fn main() {
    let cli = Cli::parse();
    let result = if matches!(cli.command, Command::SampleWorker) {
        collector::worker_main_sync()
    } else {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .context("create runtime")
            .and_then(|runtime| runtime.block_on(run(cli)))
    };
    if let Err(e) = result {
        eprintln!("{}", json!({"error":e.to_string()}));
        std::process::exit(2);
    }
}
async fn run(cli: Cli) -> Result<()> {
    if matches!(cli.command, Command::SampleWorker) {
        return collector::worker_main().await;
    }
    let pool = SessionPool::new();
    match &cli.command {
        Command::Doctor {
            endpoint,
            thread,
            surface,
            require_consumer,
        } => {
            validate_endpoint(endpoint)?;
            if let Some(thread) = thread {
                let detail = pool.check_target(endpoint, thread).await?;
                let ready = endpoint != "shared-local";
                output(
                    json!({"ready":!require_consumer||ready,"queue_api":{"ready":true},"consumer_ready":if ready{json!(true)}else{json!("unknown")},"requested_surface":surface,"client_ui_verified":false,"experimental_api":true,"detail":detail}),
                );
                pool.close().await;
                if *require_consumer && !ready {
                    bail!(
                        "shared-local cannot verify owner presence; unloaded Desktop tasks do not self-start"
                    );
                }
            } else {
                pool.call(endpoint, "thread/loaded/list", json!({})).await?;
                pool.close().await;
                output(json!({"ready":false,"level":"endpoint-only"}));
                bail!("provide --thread to check the exact target");
            }
            return Ok(());
        }
        Command::Host { port } => {
            if *port == 0 {
                bail!("port must be nonzero");
            }
            return child(vec![
                "app-server".into(),
                "--listen".into(),
                format!("ws://127.0.0.1:{port}"),
            ])
            .await;
        }
        Command::Connect {
            endpoint,
            thread,
            cwd,
        } => return connect(endpoint, thread.as_deref(), cwd.as_deref()).await,
        Command::Resident { endpoint, thread } => {
            return runtime::resident(pool, endpoint, thread).await;
        }
        _ => {}
    }
    let root = std::path::absolute(cli.state.unwrap_or_else(|| {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
            .join(".local/state/codex-monitor-rust")
    }))?;
    if root.join("monitor.sqlite3").exists() {
        bail!(
            "Python state detected; use a separate Rust directory. Automatic migration is unavailable"
        );
    }
    if let Command::Init { port } = cli.command {
        if port == 0 {
            bail!("port must be nonzero");
        }
        fs::create_dir_all(&root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
        }
        let _lock = lock(&root, "config.lock")?;
        if root.join("config.json").exists() {
            bail!("already initialized; credentials preserved");
        }
        let c = Config {
            version: 1,
            runtime: "rust".into(),
            port,
            sources: BTreeMap::new(),
        };
        private_write(
            &root.join("admin.token"),
            format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            )
            .as_bytes(),
        )?;
        Store::open(&root.join("rust.sqlite3"))?;
        private_write(&root.join("config.json"), &serde_json::to_vec_pretty(&c)?)?;
        output(json!({"state":root,"runtime":"rust","port":port,"started":false}));
        return Ok(());
    }
    let mut cfg = config(&root)?;
    let store = Store::open(&root.join("rust.sqlite3"))?;
    match cli.command {
        Command::Source { name } => {
            if !valid_name(&name) {
                bail!("invalid source name");
            }
            let _lock = lock(&root, "config.lock")?;
            cfg = config(&root)?;
            if cfg.sources.contains_key(&name) {
                bail!("source exists");
            }
            let path = root.join(format!("source-{name}.token"));
            if path.exists() {
                bail!("credential path exists");
            }
            private_write(
                &path,
                format!(
                    "{}{}",
                    uuid::Uuid::new_v4().simple(),
                    uuid::Uuid::new_v4().simple()
                )
                .as_bytes(),
            )?;
            cfg.sources.insert(
                name.clone(),
                Source {
                    token_file: path.clone(),
                },
            );
            private_write(&root.join("config.json"), &serde_json::to_vec_pretty(&cfg)?)?;
            output(
                json!({"source":name,"token_file":path,"note":"restart receiver to reload sources"}),
            );
        }
        Command::Bind {
            name,
            thread,
            source,
            endpoint,
        } => {
            validate_endpoint(&endpoint)?;
            if source.iter().any(|s| !cfg.sources.contains_key(s)) {
                bail!("register all sources first");
            }
            output(store.bind(&name, &thread, &endpoint, &source)?);
        }
        Command::Conversation { action } => output(match action {
            Conversation::Set {
                thread,
                project,
                name,
            } => store.metadata_set(&thread, &project, &name)?,
            Conversation::List { thread } => {
                json!({"conversations":store.metadata_list(thread.as_deref())?})
            }
        }),
        Command::Serve => {
            let _lock = lock(&root, "serve.lock")?;
            store.recover_submitting()?;
            runtime::serve(
                store,
                cfg,
                root,
                pool,
                Arc::new(Notify::new()),
                CancellationToken::new(),
            )
            .await?;
        }
        Command::Status => output(runtime::snapshot(&store, &cfg, &root).await?),
        Command::Sessions { name, .. } => {
            let mut routes = store.bindings()?;
            if let Some(name) = name {
                routes
                    .as_array_mut()
                    .context("routes")?
                    .retain(|v| v["name"] == name);
            }
            output(
                json!({"bindings":routes,"receiver":runtime::health(&cfg,&root).await,"consumer_ready":"unknown"}),
            );
        }
        Command::Dashboard {
            once,
            json,
            thread,
            interval,
            color,
            no_animate,
        } => {
            if json && !once {
                bail!("--json requires --once");
            }
            if !interval.is_finite() || !(0.25..=60.0).contains(&interval) {
                bail!("interval must be 0.25..60");
            }
            ui::dashboard(
                store,
                cfg,
                root,
                once,
                json,
                thread,
                Duration::from_secs_f64(interval),
                color,
                no_animate,
            )
            .await?;
        }
        Command::Monitor { action } => output(match action {
            Watch::Create {
                name,
                thread,
                file,
                interval,
                endpoint,
                debounce,
                json_pointer,
                operator,
                value,
            } => {
                validate_endpoint(&endpoint)?;
                if !file.is_absolute() {
                    bail!("file must be absolute");
                }
                let condition = match (json_pointer, operator, value) {
                    (None, None, None) => None,
                    (Some(p), Some(o), Some(v)) => Some(
                        json!({"pointer":p,"operator":o,"value":serde_json::from_str::<Value>(&v)?}),
                    ),
                    _ => bail!("provide pointer, operator and value together"),
                };
                store.watch_create(
                    &thread,
                    &name,
                    file.to_str().context("UTF-8 path required")?,
                    interval,
                    &endpoint,
                    debounce,
                    condition,
                )?
            }
            Watch::List { thread } => json!({"monitors":store.watches(thread.as_deref())?}),
            Watch::Status { name, thread } => store
                .watches(Some(&thread))?
                .as_array()
                .context("watches")?
                .iter()
                .find(|v| v["name"] == name && v["removed"] != true)
                .context("unknown watch")?
                .clone(),
            Watch::Pause { name, thread } => store.watch_action(&thread, &name, "pause")?,
            Watch::Resume { name, thread } => store.watch_action(&thread, &name, "resume")?,
            Watch::Remove { name, thread } => store.watch_action(&thread, &name, "remove")?,
        }),
        Command::Event { id } => output(store.event(&id)?),
        Command::Inspect { id } => {
            let e = store.event(&id)?;
            let routes = store.bindings()?;
            let b = routes
                .as_array()
                .context("routes")?
                .iter()
                .find(|b| b["name"] == e["binding"])
                .context("binding unavailable")?;
            let native = pool
                .inspect(
                    text(b, "endpoint")?,
                    text(b, "thread")?,
                    text(&e, "client_id")?,
                )
                .await;
            output(
                json!({"local":e,"native":native.unwrap_or_else(|_|json!({"state":"unknown","reason":"inspection unavailable"})),"task_success":"not_inferred"}),
            );
            pool.close().await;
        }
        Command::Pause { binding } => output(store.route_action(&binding, "pause")?),
        Command::Unpause { binding } => output(store.route_action(&binding, "resume")?),
        Command::Remove { binding } => output(store.route_action(&binding, "remove")?),
        Command::Request { action } => output(match action {
            Request::Create {
                thread,
                source,
                id,
                delivery,
                payload,
                expires_at,
            } => store.request_create(
                &thread,
                &source,
                &id,
                &delivery,
                &serde_json::from_str::<Value>(&read_input(&payload)?)?,
                expires_at,
            )?,
            Request::Update {
                thread,
                source,
                id,
                update_id,
                status,
                expected_revision,
                detail,
            } => store.request_update(
                &thread,
                &source,
                &id,
                &update_id,
                &status,
                expected_revision,
                detail.as_deref(),
            )?,
            Request::List { thread, source } => store.requests(&thread, source.as_deref())?,
        }),
        Command::Reply {
            delivery_id,
            id,
            message,
        } => output(store.reply_put(&delivery_id, &id, &read_input(&message)?)?),
        Command::Send {
            to,
            source,
            id,
            r#type,
            data,
            url,
            token_env,
            trace,
            hops,
        } => {
            let url = url.unwrap_or_else(|| format!("http://127.0.0.1:{}", cfg.port));
            runtime::validate_ingress_url(&url)?;
            let secret = if let Some(n) = token_env {
                std::env::var(n).context("token environment unavailable")?
            } else {
                token(
                    &cfg.sources
                        .get(&source)
                        .context("unknown source")?
                        .token_file,
                )?
            };
            let mut e = json!({"id":id,"source":source,"type":r#type,"data":serde_json::from_str::<Value>(&read_input(&data)?)?,"hops":hops});
            if let Some(t) = trace {
                e["trace_id"] = json!(t);
            }
            if !valid_name(&to) {
                bail!("invalid route");
            }
            let r = runtime::client()?
                .post(format!("{}/v1/events/{to}", url.trim_end_matches('/')))
                .bearer_auth(secret)
                .json(&e)
                .send()
                .await?;
            let status = r.status();
            output(r.json().await?);
            if !status.is_success() {
                bail!("receiver rejected event ({status}); preserve the event ID");
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}
async fn child(args: Vec<String>) -> Result<()> {
    let s = tokio::process::Command::new("codex")
        .args(args)
        .kill_on_drop(true)
        .status()
        .await
        .context("launch codex")?;
    if !s.success() {
        bail!("Codex exited with {s}");
    }
    Ok(())
}
pub async fn connect(endpoint: &str, thread: Option<&str>, cwd: Option<&Path>) -> Result<()> {
    validate_endpoint(endpoint)?;
    if !endpoint.starts_with("ws://")
        && !endpoint.starts_with("wss://")
        && !endpoint.starts_with("unix://")
    {
        bail!("TUI requires an explicit ws/wss/Unix owner endpoint");
    }
    let mut args = vec!["--remote".into(), endpoint.into()];
    let secret = codex_monitor_rs::session::server_token()?;
    if secret.is_some() {
        args.extend([
            "--remote-auth-token-env".into(),
            "CODEX_MONITOR_SERVER_TOKEN".into(),
        ]);
    }
    if let Some(cwd) = cwd {
        args.extend(["-C".into(), cwd.to_string_lossy().into_owned()]);
    }
    if let Some(t) = thread {
        args.extend(["resume".into(), t.into()]);
    }
    let mut cmd = tokio::process::Command::new("codex");
    cmd.args(args).kill_on_drop(true);
    if let Some(secret) = secret {
        cmd.env("CODEX_MONITOR_SERVER_TOKEN", secret);
    }
    let result = cmd.status().await.context("launch Codex TUI")?;
    if !result.success() {
        bail!("Codex exited with {result}");
    }
    Ok(())
}
