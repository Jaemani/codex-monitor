//! Explicit shared-owner recovery. Never creates turns or replays failed input.
mod systemd;

use anyhow::{Context, Result, bail};
use codex_monitor_rs::session::SessionPool;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::fs::MetadataExt;
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

#[derive(Clone, Debug, PartialEq)]
pub struct Job {
    pub path: PathBuf,
    pub raw: Vec<u8>,
    pub label: String,
    pub args: Vec<String>,
    pub env: std::collections::BTreeMap<String, String>,
    pub cwd: Option<String>,
}
#[derive(Clone)]
pub struct Plan {
    pub endpoint: String,
    pub thread: String,
    pub owner: Job,
    pub residents: Vec<Job>,
    pub threads: BTreeSet<String>,
    pub pid: u32,
    pub created: Instant,
    pub policy: Option<String>,
}
pub fn option(args: &[String], key: &str) -> Option<String> {
    let values: Vec<_> = args
        .windows(2)
        .filter(|w| w[0] == key)
        .map(|w| w[1].clone())
        .collect();
    if values.len() == 1 {
        values.into_iter().next()
    } else {
        None
    }
}
pub fn jobs() -> Result<Vec<Job>> {
    if cfg!(target_os = "linux") {
        return systemd::jobs();
    }
    if !cfg!(target_os = "macos") {
        bail!("Service reconnect requires macOS LaunchAgents or Linux systemd user services");
    }
    let dir = PathBuf::from(std::env::var("HOME")?).join("Library/LaunchAgents");
    let mut paths: Vec<_> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    paths.sort();
    let mut out = Vec::new();
    for path in paths.into_iter().take(256) {
        if path.extension().and_then(|s| s.to_str()) != Some("plist") {
            continue;
        }
        let Ok(meta) = path.symlink_metadata() else {
            continue;
        };
        if meta.file_type().is_symlink()
            || meta.uid() != unsafe { libc::getuid() }
            || meta.mode() & 0o022 != 0
            || meta.len() > 131072
        {
            continue;
        }
        let Ok(raw) = std::fs::read(&path) else {
            continue;
        };
        let Ok(v) = plist::from_bytes::<Value>(&raw) else {
            continue;
        };
        let Some(label) = v["Label"].as_str().filter(|s| {
            !s.is_empty()
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        }) else {
            continue;
        };
        let Some(args) = v["ProgramArguments"]
            .as_array()
            .and_then(|a| {
                a.iter()
                    .map(|x| x.as_str().map(str::to_owned))
                    .collect::<Option<Vec<_>>>()
            })
            .filter(|a| !a.is_empty())
        else {
            continue;
        };
        let env = match v.get("EnvironmentVariables") {
            Some(v) => match serde_json::from_value(v.clone()) {
                Ok(e) => e,
                Err(_) => continue,
            },
            None => Default::default(),
        };
        out.push(Job {
            path,
            raw,
            label: label.into(),
            args,
            env,
            cwd: v["WorkingDirectory"].as_str().map(str::to_owned),
        });
    }
    Ok(out)
}
fn domain(job: &Job) -> String {
    format!("gui/{}/{}", unsafe { libc::getuid() }, job.label)
}
async fn launch(args: &[&str]) -> Result<String> {
    let mut cmd = tokio::process::Command::new("/bin/launchctl");
    cmd.args(args).kill_on_drop(true);
    let out = tokio::time::timeout(Duration::from_secs(5), cmd.output()).await??;
    if !out.status.success() {
        bail!("Configured owner or resident service is unavailable");
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}
async fn pid(job: &Job) -> Result<u32> {
    if cfg!(target_os = "linux") {
        return systemd::pid(job).await;
    }
    let out = launch(&["print", &domain(job)]).await?;
    let lines: Vec<_> = out.lines().map(str::trim).collect();
    if !lines.contains(&format!("path = {}", job.path.display()).as_str()) {
        bail!("Loaded service path differs from saved configuration");
    }
    let start = lines
        .iter()
        .position(|s| *s == "arguments = {")
        .context("Loaded arguments unavailable")?;
    let args: Vec<_> = lines[start + 1..]
        .iter()
        .take_while(|s| **s != "}")
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    if args != job.args {
        bail!("Loaded service arguments differ from saved configuration");
    }
    lines
        .iter()
        .find_map(|l| l.strip_prefix("pid = ").and_then(|p| p.parse().ok()))
        .context("Service PID unavailable")
}
pub fn configuration(endpoint: &str) -> Result<(Job, Vec<Job>)> {
    let local = endpoint.starts_with("unix:///")
        || url::Url::parse(endpoint).ok().is_some_and(|u| {
            u.scheme() == "ws" && matches!(u.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
        });
    if !local {
        bail!("Reconnect requires an explicit local owner endpoint");
    }
    let all = jobs()?;
    let owners: Vec<_> = all
        .iter()
        .filter(|j| {
            Path::new(&j.args[0]).file_name().and_then(|s| s.to_str()) == Some("codex")
                && j.args.iter().any(|a| a == "app-server")
                && !j.args.iter().any(|a| a == "proxy")
                && option(&j.args, "--listen").as_deref() == Some(endpoint)
        })
        .cloned()
        .collect();
    if owners.len() != 1 {
        bail!("No unique direct owner service matches this endpoint");
    }
    let owner = owners[0].clone();
    let home = PathBuf::from(std::env::var("HOME")?);
    let expected = PathBuf::from(
        std::env::var("CODEX_HOME")
            .unwrap_or_else(|_| home.join(".codex").to_string_lossy().into_owned()),
    );
    let actual = PathBuf::from(owner.env.get("CODEX_HOME").cloned().unwrap_or_else(|| {
        PathBuf::from(
            owner
                .env
                .get("HOME")
                .map(String::as_str)
                .unwrap_or(home.to_str().unwrap_or("")),
        )
        .join(".codex")
        .to_string_lossy()
        .into_owned()
    }));
    if expected.canonicalize()? != actual.canonicalize()?
        || !Path::new(&owner.args[0]).is_absolute()
    {
        bail!("Owner login environment differs; reconnect from its Codex home");
    }
    let residents = all
        .into_iter()
        .filter(|j| {
            matches!(
                Path::new(&j.args[0]).file_name().and_then(|s| s.to_str()),
                Some("codex-monitor" | "codex-monitor-rs")
            ) && j.args.iter().any(|a| a == "resident")
                && option(&j.args, "--endpoint").as_deref() == Some(endpoint)
        })
        .collect::<Vec<_>>();
    if residents.is_empty() {
        bail!("No resident services cover this owner");
    }
    Ok((owner, residents))
}
fn configured(jobs: &[Job]) -> BTreeSet<String> {
    jobs.iter()
        .flat_map(|j| {
            j.args
                .windows(2)
                .filter(|w| w[0] == "--thread")
                .map(|w| w[1].clone())
        })
        .collect()
}
async fn loaded(pool: &SessionPool, endpoint: &str) -> Result<BTreeSet<String>> {
    let mut threads = BTreeSet::new();
    let mut cursor = Value::Null;
    let mut seen = Vec::new();
    for _ in 0..8 {
        let v = pool
            .call(
                endpoint,
                "thread/loaded/list",
                if cursor.is_null() {
                    json!({})
                } else {
                    json!({"cursor":cursor})
                },
            )
            .await?;
        for t in v["data"].as_array().context("Invalid loaded inventory")? {
            threads.insert(t.as_str().context("Invalid loaded thread")?.to_owned());
        }
        cursor = v["nextCursor"].clone();
        if cursor.is_null() || cursor == "" {
            return Ok(threads);
        }
        if seen.contains(&cursor) {
            break;
        }
        seen.push(cursor.clone());
    }
    bail!("Owner inventory exceeds bounded inspection");
}
pub async fn plan(endpoint: &str, thread: &str, policy: Option<&str>) -> Result<Plan> {
    if policy.is_some_and(|p| !["full", "read-only", "workspace-network"].contains(&p)) {
        bail!("Unsupported permission mode");
    }
    let (owner, residents) = configuration(endpoint)?;
    let old_pid = pid(&owner).await?;
    for j in &residents {
        pid(j).await?;
    }
    let pool = SessionPool::new();
    let inspected = tokio::time::timeout(Duration::from_secs(15), async {
        let threads = loaded(&pool, endpoint).await?;
        if threads.len() > 32 {
            bail!("At most 32 loaded conversations can be reconnected");
        }
        for id in &threads {
            let v = pool
                .call(
                    endpoint,
                    "thread/read",
                    json!({"threadId":id,"includeTurns":false}),
                )
                .await?;
            let status = &v["thread"]["status"];
            let status = status
                .as_str()
                .or_else(|| status["type"].as_str())
                .unwrap_or("");
            if !["idle", "systemError"].contains(&status) {
                bail!("A conversation is running, awaiting approval/input, or unverified. Open it in Codex and resolve pending decisions; retry when all conversations are idle");
            }
        }
        let covered = configured(&residents);
        if !threads.contains(thread) || !threads.is_subset(&covered) {
            bail!("Resident services do not cover every loaded conversation");
        }
        if policy.is_some() {
            for id in covered.union(&threads) {
                let q = pool
                    .call(
                        endpoint,
                        "thread/queue/list",
                        json!({"threadId":id,"limit":1}),
                    )
                    .await?;
                if !q["data"].as_array().is_some_and(|a| a.is_empty()) {
                    bail!("Permission changes require empty queues on all affected conversations");
                }
            }
        }
        Ok(threads)
    })
    .await;
    pool.close().await;
    Ok(Plan {
        endpoint: endpoint.into(),
        thread: thread.into(),
        owner,
        residents,
        threads: inspected??,
        pid: old_pid,
        created: Instant::now(),
        policy: policy.map(str::to_owned),
    })
}
impl Plan {
    pub fn summary(&self) -> String {
        let ids = configured(&self.residents)
            .into_iter()
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "SHARED SERVER {}: {}. Affects ALL conversations: {}. Existing terminals disconnect. {} Select the SAME action again within 60 seconds to confirm.",
            self.endpoint,
            self.policy.as_deref().unwrap_or("Reconnect current login"),
            ids,
            if self.policy.is_some() {
                "Service permissions persist until changed; this is not a per-conversation change."
            } else {
                "Permissions remain unchanged; no failed input is replayed."
            }
        )
    }
}
pub fn policy_args(args: &[String], policy: &str, resident: bool) -> Result<Vec<String>> {
    let mode = match policy {
        "full" => "danger-full-access",
        "read-only" => "read-only",
        "workspace-network" => "workspace-write",
        _ => bail!("Unsupported permission mode"),
    };
    let mut out = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if [
            "--dangerously-bypass-approvals-and-sandbox",
            "--yolo",
            "--full-auto",
        ]
        .contains(&a.as_str())
        {
            bail!(
                "Normalize combined approval/sandbox flags before changing permissions; approval policy must remain explicit"
            );
        }
        if ["--sandbox", "-s"].contains(&a.as_str()) {
            if i + 1 >= args.len() {
                bail!("Malformed sandbox option");
            }
            i += 2;
            continue;
        }
        if a.starts_with("--sandbox=")
            || [
                "--network-access",
                "--no-network-access",
                "--dangerously-bypass-approvals-and-sandbox",
                "--yolo",
                "--full-auto",
            ]
            .contains(&a.as_str())
        {
            i += 1;
            continue;
        }
        if !resident && ["-c", "--config"].contains(&a.as_str()) {
            let v = args.get(i + 1).context("Malformed owner config")?;
            let key = v.split('=').next().unwrap_or("").trim();
            if ![
                "sandbox_mode",
                "sandbox_workspace_write.network_access",
                "default_permissions",
                "permissions",
            ]
            .contains(&key)
                && !key.starts_with("permissions.")
            {
                out.extend([a.clone(), v.clone()]);
            }
            i += 2;
            continue;
        }
        if !resident && (a.starts_with("--config=") || a.starts_with("-c") && a != "-c") {
            bail!("Normalize compact owner config before changing permissions");
        }
        out.push(a.clone());
        i += 1;
    }
    if resident {
        out.extend(["--sandbox".into(), mode.into()]);
        if policy == "workspace-network" {
            out.push("--network-access".into());
        }
    } else {
        out.splice(
            1..1,
            [
                "-c".into(),
                format!("sandbox_mode=\"{mode}\""),
                "-c".into(),
                format!(
                    "sandbox_workspace_write.network_access={}",
                    policy == "workspace-network"
                ),
            ],
        );
    }
    Ok(out)
}
async fn fresh_account(plan: &Plan) -> Result<Value> {
    let temp = std::env::temp_dir().join(format!("cm-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir(&temp)?;
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o700))?;
    let endpoint = format!("unix://{}", temp.join("owner.sock").display());
    let mut args = plan.owner.args.clone();
    let i = args
        .iter()
        .position(|s| s == "--listen")
        .context("Missing owner endpoint")?;
    args[i + 1] = endpoint.clone();
    let mut cmd = tokio::process::Command::new(&args[0]);
    cmd.args(&args[1..])
        .envs(&plan.owner.env)
        .kill_on_drop(true)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    if let Some(cwd) = &plan.owner.cwd {
        cmd.current_dir(cwd);
    }
    let mut child = cmd.spawn()?;
    let pool = SessionPool::new();
    let result=tokio::time::timeout(Duration::from_secs(20),async {
        let mut account=None;
        for _ in 0..40 {if let Ok(v)=pool.call(&endpoint,"account/read",json!({"refreshToken":false})).await {account=Some(v["account"].clone());break;}tokio::time::sleep(Duration::from_millis(100)).await;}
        let account=account.context("Fresh login unavailable; sign in first")?;
        if account["type"]!="chatgpt" || !account["email"].is_string(){bail!("Current ChatGPT login cannot be verified");}
        let limits=pool.call(&endpoint,"account/rateLimits/read",json!({})).await?;
        if !limits["rateLimits"].is_object() && !limits["rateLimitsByLimitId"].is_object(){bail!("Current login access unverified");}
        for id in &plan.threads {
            let v=pool.call(&endpoint,"thread/read",json!({"threadId":id,"includeTurns":false})).await?;
            if v["thread"]["id"]!=*id {bail!("Saved conversation missing");}
            let t=&v["thread"];let provider=t["modelProvider"].as_str().context("Saved provider unknown")?;
            if provider!="openai" {
                let cwd=t["cwd"].as_str().context("Saved project unknown")?;
                let c=pool.call(&endpoint,"config/read",json!({"cwd":cwd,"includeLayers":false})).await?;
                if !c["config"]["model_providers"][provider].as_object().is_some_and(|o|!o.is_empty()){bail!("Saved provider is absent; restore it or explicitly change that conversation's provider");}
            }
        } Ok(account)
    }).await;
    pool.close().await;
    let _ = child.kill().await;
    let _ = child.wait().await;
    let _ = std::fs::remove_dir_all(temp);
    result?
}
async fn stop(job: &Job) -> Result<()> {
    if cfg!(target_os = "linux") {
        return systemd::stop(job).await;
    }
    launch(&["bootout", &domain(job)]).await?;
    Ok(())
}
async fn reload() -> Result<()> {
    if cfg!(target_os = "linux") {
        systemd::reload().await?;
    }
    Ok(())
}
async fn bootstrap(job: &Job) -> Result<()> {
    if cfg!(target_os = "linux") {
        return systemd::start(job).await;
    }
    for i in 0..4 {
        if launch(&[
            "bootstrap",
            &format!("gui/{}", unsafe { libc::getuid() }),
            job.path.to_str().context("Invalid path")?,
        ])
        .await
        .is_ok()
            || pid(job).await.is_ok()
        {
            return Ok(());
        }
        if i < 3 {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
    bail!("Service bootstrap failed")
}
async fn verify(plan: &Plan, owner: &Job, expected: &Value) -> Result<()> {
    let pool = SessionPool::new();
    let result = tokio::time::timeout(Duration::from_secs(35), async {
        loop {
            let attempt = async {
                let current = pid(owner).await?;
                if current == plan.pid {
                    bail!("Waiting for owner restart");
                }
                let v = pool
                    .call(
                        &plan.endpoint,
                        "account/read",
                        json!({"refreshToken":false}),
                    )
                    .await?;
                if &v["account"] != expected {
                    bail!("Restarted owner account differs from verified login");
                }
                if !plan
                    .threads
                    .is_subset(&loaded(&pool, &plan.endpoint).await?)
                {
                    bail!("Waiting for resident subscriptions");
                }
                let limits = pool
                    .call(&plan.endpoint, "account/rateLimits/read", json!({}))
                    .await?;
                if !limits["rateLimits"].is_object() && !limits["rateLimitsByLimitId"].is_object() {
                    bail!("Account access unverified");
                }
                if let Some(policy) = &plan.policy {
                    for id in &configured(&plan.residents) {
                        let response = pool
                            .call(
                                &plan.endpoint,
                                "thread/resume",
                                json!({"threadId":id,"excludeTurns":true}),
                            )
                            .await?;
                        let expected = match policy.as_str() {
                            "full" => "dangerFullAccess",
                            "read-only" => "readOnly",
                            _ => "workspaceWrite",
                        };
                        if response["sandbox"]["type"] != expected
                            || policy == "workspace-network"
                                && response["sandbox"]["networkAccess"].as_bool() != Some(true)
                            || policy == "read-only"
                                && response["sandbox"]["networkAccess"]
                                    .as_bool()
                                    .unwrap_or(false)
                        {
                            bail!("Effective permissions differ from selected mode");
                        }
                    }
                }
                Ok(())
            }
            .await;
            if attempt.is_ok() {
                return attempt;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    })
    .await;
    pool.close().await;
    result.context("Restart verification timed out; inspect services before retrying")?
}
pub async fn execute(root: &Path, plan: &Plan) -> Result<String> {
    if plan.created.elapsed() > Duration::from_secs(60) {
        bail!("Preview expired; inspect again");
    }
    let dir = PathBuf::from(std::env::var("HOME")?).join(".cache/codex-monitor");
    std::fs::create_dir_all(&dir)?;
    let _lock = crate::lock(
        &dir,
        &format!(
            "{:x}.reconnect.lock",
            Sha256::digest(plan.endpoint.as_bytes())
        ),
    )?;
    let expected = fresh_account(plan).await?;
    let fresh = self::plan(&plan.endpoint, &plan.thread, plan.policy.as_deref()).await?;
    if fresh.owner != plan.owner
        || fresh.residents != plan.residents
        || fresh.threads != plan.threads
        || fresh.pid != plan.pid
    {
        bail!("Owner changed since preview; inspect again");
    }
    if let Some(policy) = &plan.policy {
        let mut all = vec![plan.owner.clone()];
        all.extend(plan.residents.clone());
        let mut changed = Vec::new();
        for (i, job) in all.iter().enumerate() {
            if std::fs::read(&job.path)? != job.raw {
                bail!("Saved service changed");
            }
            let mut next = job.clone();
            next.args = policy_args(&job.args, policy, i > 0)?;
            next.raw = if cfg!(target_os = "linux") {
                systemd::rewrite(job, &next.args)?
            } else {
                let mut spec: Value = plist::from_bytes(&job.raw)?;
                spec["ProgramArguments"] = json!(next.args);
                let mut raw = Vec::new();
                plist::to_writer_xml(&mut raw, &spec)?;
                raw
            };
            changed.push(next);
        }
        for job in &plan.residents {
            let mut command = tokio::process::Command::new(&job.args[0]);
            command.args(["resident", "--help"]).kill_on_drop(true);
            let result = tokio::time::timeout(Duration::from_secs(5), command.output()).await??;
            let help = String::from_utf8_lossy(&result.stdout);
            if !result.status.success()
                || !help.contains("--sandbox")
                || policy == "workspace-network" && !help.contains("--network-access")
            {
                bail!("Upgrade the resident executable before changing permissions");
            }
        }
        let backup = root
            .join("permission-backups")
            .join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir_all(&backup)?;
        for job in &all {
            crate::private_write(
                &backup.join(job.path.file_name().context("Invalid service path")?),
                &job.raw,
            )?;
        }
        let mut stopped = Vec::new();
        let result: Result<()> = async {
            for job in plan.residents.iter().chain(std::iter::once(&plan.owner)) {
                stopped.push(job.clone());
                stop(job).await?;
            }
            for job in &changed {
                crate::private_write(&job.path, &job.raw)?;
            }
            reload().await?;
            for job in &changed {
                bootstrap(job).await?;
            }
            verify(plan, &changed[0], &expected).await
        }
        .await;
        if let Err(error) = result {
            let mut restored = true;
            for job in stopped.iter().rev() {
                restored &= stop(job).await.is_ok();
            }
            for job in &all {
                restored &= crate::private_write(&job.path, &job.raw).is_ok();
            }
            restored &= reload().await.is_ok();
            for job in &all {
                if stopped.contains(job) {
                    restored &= bootstrap(job).await.is_ok();
                }
            }
            bail!(
                "{error}. Configuration restoration {}. Inspect effective state before retrying. Backup: {}",
                if restored { "completed" } else { "incomplete" },
                backup.display()
            );
        }
        return Ok(format!(
            "Shared server permissions changed for {} conversations. Settings persist. No input replayed. Backup: {}",
            configured(&plan.residents).len(),
            backup.display()
        ));
    }
    // A command timeout may follow restart acceptance. Reconcile once; never retry restart.
    if cfg!(target_os = "linux") {
        let _ = systemd::restart(&plan.owner).await;
    } else {
        let _ = launch(&["kickstart", "-k", &domain(&plan.owner)]).await;
    }
    verify(plan, &plan.owner, &expected).await?;
    Ok(format!(
        "Reconnected with current login; {} conversations restored. No failed input replayed.",
        plan.threads.len()
    ))
}
pub fn permission_label(args: &[String]) -> &'static str {
    match option(args, "--sandbox").as_deref() {
        Some("danger-full-access") => "Full Access",
        Some("read-only") => "Read-only",
        Some("workspace-write") if args.iter().any(|s| s == "--network-access") => "Project Access",
        Some("workspace-write") => "Workspace",
        _ => "Access unknown",
    }
}
fn owner_permission(args: &[String]) -> &'static str {
    let mut mode = None;
    let mut network = None;
    for pair in args
        .windows(2)
        .filter(|w| ["-c", "--config"].contains(&w[0].as_str()))
    {
        if let Some((key, value)) = pair[1].split_once('=') {
            let key = key.trim();
            let value = value.trim().trim_matches(['\"', '\'']);
            match key {
                "sandbox_mode" => mode = Some(value),
                "sandbox_workspace_write.network_access" => network = Some(value),
                "default_permissions" | "permissions" => return "Access unknown",
                _ if key.starts_with("permissions.") => return "Access unknown",
                _ => {}
            }
        }
    }
    if args.iter().any(|s| {
        s == "--profile"
            || s == "-p"
            || s.starts_with("--config=")
            || s.starts_with("--profile=")
            || s.starts_with("-c") && s != "-c"
    }) {
        return "Access unknown";
    }
    match mode {
        Some("danger-full-access") => "Full Access",
        Some("read-only") => "Read-only",
        Some("workspace-write") if network == Some("true") => "Project Access",
        Some("workspace-write") => "Workspace",
        _ => "Access unknown",
    }
}
fn attach_saved_permissions(bindings: &mut Value, metadata: &Value, all: &[Job]) {
    if let Some(rows) = bindings.as_array_mut() {
        for b in rows {
            let endpoint = b["endpoint"].as_str();
            let residents: Vec<_> = all
                .iter()
                .filter(|j| {
                    matches!(
                        Path::new(&j.args[0]).file_name().and_then(|s| s.to_str()),
                        Some("codex-monitor" | "codex-monitor-rs")
                    ) && j.args.iter().any(|a| a == "resident")
                        && option(&j.args, "--endpoint").as_deref() == endpoint
                })
                .cloned()
                .collect();
            let owners: Vec<_> = all
                .iter()
                .filter(|j| {
                    Path::new(&j.args[0]).file_name().and_then(|s| s.to_str()) == Some("codex")
                        && j.args.iter().any(|a| a == "app-server")
                        && !j.args.iter().any(|a| a == "proxy")
                        && option(&j.args, "--listen").as_deref() == endpoint
                })
                .collect();
            let inherited = if owners.len() == 1 {
                owner_permission(&owners[0].args)
            } else {
                "Access unknown"
            };
            let labels: BTreeSet<_> = residents
                .iter()
                .filter(|j| {
                    j.args
                        .windows(2)
                        .any(|w| w[0] == "--thread" && Some(w[1].as_str()) == b["thread"].as_str())
                })
                .map(|j| {
                    if option(&j.args, "--sandbox").is_some() {
                        permission_label(&j.args)
                    } else {
                        inherited
                    }
                })
                .collect();
            let affected: Vec<_> = configured(&residents).into_iter().map(|id| {
                let info = metadata.as_array().and_then(|a| a.iter().find(|m| m["thread"] == id));
                json!({"thread":id,"project":info.and_then(|m|m["project"].as_str()).unwrap_or("Ungrouped"),"name":info.and_then(|m|m["display_name"].as_str()).unwrap_or(&id)})
            }).collect();
            let projects: BTreeSet<_> = affected
                .iter()
                .filter_map(|a| a["project"].as_str())
                .collect();
            let scope = if projects.len() == 1 && !projects.contains("Ungrouped") {
                "project"
            } else {
                "server"
            };
            b["permission"] = json!({
                "label":if labels.len()==1{*labels.first().unwrap()}else if labels.is_empty(){"Access unknown"}else{"Check permissions"},
                "source":"saved-service-config","effective_verified":false,
                "inherited_owner_label":inherited,"scope":scope,"projects":projects,"affected":affected
            });
        }
    }
}
pub fn attach_permissions(bindings: &mut Value, metadata: &Value) {
    attach_saved_permissions(bindings, metadata, &jobs().unwrap_or_default());
}

pub fn permission_scope(permission: &Value) -> String {
    let projects = permission["projects"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let names = permission["affected"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|r| r["name"].as_str())
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    format!(
        "Shared server scope: {projects}. Affected conversations: {names}. All loaded conversations must be idle; approval policy and OS sudo privileges are not changed."
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(args: &[&str]) -> Job {
        Job {
            path: "/tmp/fixture.service".into(),
            raw: vec![],
            label: "fixture.service".into(),
            args: args.iter().map(|s| s.to_string()).collect(),
            env: Default::default(),
            cwd: None,
        }
    }
    #[test]
    fn inherited_permissions_and_shared_scope_do_not_depend_on_selected_route() {
        let owner = fixture(&[
            "/bin/codex",
            "-c",
            "sandbox_mode=\"workspace-write\"",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "app-server",
            "--listen",
            "ws://localhost:1",
        ]);
        let resident = fixture(&[
            "/bin/codex-monitor",
            "resident",
            "--endpoint",
            "ws://localhost:1",
            "--thread",
            "one",
            "--thread",
            "two",
        ]);
        let mut bindings = json!([{"thread":"one","endpoint":"ws://localhost:1"}]);
        let mut metadata = json!([{"thread":"one","project":"Project","display_name":"One"},{"thread":"two","project":"Project","display_name":"Two"}]);
        let mut jobs = vec![owner, resident];
        attach_saved_permissions(&mut bindings, &metadata, &jobs);
        let p = &bindings[0]["permission"];
        assert_eq!(p["label"], "Project Access");
        assert_eq!(p["effective_verified"], false);
        assert_eq!(p["scope"], "project");
        assert_eq!(p["affected"].as_array().unwrap().len(), 2);
        metadata[1]["project"] = json!("Other project");
        attach_saved_permissions(&mut bindings, &metadata, &jobs);
        assert_eq!(bindings[0]["permission"]["scope"], "server");
        jobs[1]
            .args
            .extend(["--sandbox".into(), "read-only".into()]);
        attach_saved_permissions(&mut bindings, &metadata, &jobs);
        assert_eq!(bindings[0]["permission"]["label"], "Read-only");
        metadata.as_array_mut().unwrap().pop();
        attach_saved_permissions(&mut bindings, &metadata, &jobs);
        assert_eq!(bindings[0]["permission"]["scope"], "server");
    }
    #[test]
    fn owner_profiles_remain_unverified_and_approval_policy_is_preserved() {
        let job = fixture(&[
            "/bin/codex",
            "-c",
            "sandbox_mode=\"read-only\"",
            "-c",
            "approval_policy=\"on-request\"",
            "app-server",
        ]);
        assert_eq!(owner_permission(&job.args), "Read-only");
        let changed = policy_args(&job.args, "full", false).unwrap();
        assert!(changed.contains(&"approval_policy=\"on-request\"".to_owned()));
        assert!(!changed.iter().any(|s| s.contains("never")));
        let mut args = job.args;
        args.extend(["--profile".into(), "custom".into()]);
        assert_eq!(owner_permission(&args), "Access unknown");
        args.push("--yolo".into());
        assert!(policy_args(&args, "full", false).is_err());
    }
    #[test]
    fn permission_rewrite_preserves_endpoint_and_unrelated_config() {
        let args = vec![
            "/bin/codex",
            "-c",
            "model=\"example\"",
            "-c",
            "sandbox_mode=\"read-only\"",
            "app-server",
            "--listen",
            "ws://127.0.0.1:1",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
        let changed = policy_args(&args, "workspace-network", false).unwrap();
        assert_eq!(
            option(&changed, "--listen"),
            Some("ws://127.0.0.1:1".into())
        );
        assert!(changed.contains(&"model=\"example\"".into()));
        assert_eq!(
            changed
                .iter()
                .filter(|a| a.starts_with("sandbox_mode="))
                .count(),
            1
        );
    }
    #[test]
    fn resident_modes_remove_network_when_restricted() {
        let args = vec![
            "codex-monitor",
            "resident",
            "--sandbox",
            "workspace-write",
            "--network-access",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
        let a = policy_args(&args, "read-only", true).unwrap();
        assert!(!a.contains(&"--network-access".into()));
        assert_eq!(permission_label(&a), "Read-only");
    }
}
