//! Reconnect and explicitly update verified, direct systemd user services.
use super::Job;
use anyhow::{Context, Result, bail};
use std::{
    collections::BTreeMap,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::Duration,
};

// Deliberately accept only literal unit arguments. Guessing systemd expansions could
// validate a different executable, endpoint or login than the manager will start.
fn words(value: &str) -> Result<Vec<String>> {
    if value.contains(['\\', '%', '$', '\0']) {
        bail!("Use literal systemd arguments without escapes or expansions for reconnect");
    }
    let mut out = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut started = false;
    for c in value.chars() {
        if let Some(delimiter) = quote {
            if c == delimiter {
                quote = None;
            } else {
                word.push(c);
            }
        } else if c == '\'' || c == '"' {
            quote = Some(c);
            started = true;
        } else if c.is_whitespace() {
            if started {
                out.push(std::mem::take(&mut word));
                started = false;
            }
        } else {
            word.push(c);
            started = true;
        }
    }
    if quote.is_some() {
        bail!("Unterminated systemd quoting");
    }
    if started {
        out.push(word);
    }
    Ok(out)
}

fn parse(path: PathBuf, raw: Vec<u8>) -> Result<Job> {
    let label = path
        .file_name()
        .and_then(|s| s.to_str())
        .context("Invalid unit name")?
        .to_owned();
    if !label.ends_with(".service")
        || label.starts_with('-')
        || !label
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
    {
        bail!("Reconnect requires a direct, non-template user service");
    }
    let text = std::str::from_utf8(&raw)?;
    let mut section = "";
    let mut args = None;
    let mut env = BTreeMap::new();
    let mut cwd = None;
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with(['#', ';']) {
            continue;
        }
        if line.ends_with('\\') {
            bail!("Normalize unit line continuations before reconnect");
        }
        if line.starts_with('[') && line.ends_with(']') {
            section = line;
            continue;
        }
        let (key, value) = line.split_once('=').context("Invalid unit directive")?;
        let (key, value) = (key.trim(), value.trim());
        if section != "[Service]" {
            continue;
        }
        match key {
            "ExecStart" => {
                if args.is_some() {
                    bail!("Reconnect requires exactly one ExecStart");
                }
                let parsed = words(value)?;
                if parsed.first().is_none_or(|s| !Path::new(s).is_absolute()) {
                    bail!("Reconnect requires an absolute direct executable");
                }
                args = Some(parsed);
            }
            "Environment" => {
                if value.is_empty() {
                    env.clear();
                }
                for entry in words(value)? {
                    let (k, v) = entry.split_once('=').context("Invalid unit environment")?;
                    if k.is_empty() || !k.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_') {
                        bail!("Invalid environment name");
                    }
                    env.insert(k.to_owned(), v.to_owned());
                }
            }
            "WorkingDirectory" => {
                let values = words(value)?;
                if values.len() != 1 || !Path::new(&values[0]).is_absolute() {
                    bail!("Reconnect requires an absolute working directory");
                }
                cwd = Some(values[0].clone());
            }
            "Type" if matches!(value, "simple" | "exec") => {}
            "Restart" | "RestartSec" | "TimeoutStartSec" | "TimeoutStopSec" | "KillMode"
            | "KillSignal" | "SendSIGKILL" | "StandardOutput" | "StandardError"
            | "SyslogIdentifier" | "UMask" | "MemoryHigh" | "MemoryMax" | "TasksMax"
            | "CPUQuota" => {}
            _ => bail!("Unsupported reconnect service directive: {key}"),
        }
    }
    let cwd = cwd
        .or_else(|| env.get("HOME").cloned())
        .or_else(|| std::env::var("HOME").ok());
    Ok(Job {
        path,
        raw,
        label,
        args: args.context("Missing ExecStart")?,
        env,
        cwd,
    })
}

pub(super) fn jobs() -> Result<Vec<Job>> {
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or(PathBuf::from(std::env::var("HOME")?).join(".config"));
    read_jobs(&config.join("systemd/user"))
}

fn read_jobs(dir: &Path) -> Result<Vec<Job>> {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    let mut paths = entries
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();
    let mut jobs = Vec::new();
    for path in paths {
        if path.extension().and_then(|s| s.to_str()) != Some("service") {
            continue;
        }
        let meta = path.symlink_metadata()?;
        if !meta.is_file()
            || meta.uid() != unsafe { libc::getuid() }
            || meta.mode() & 0o022 != 0
            || meta.len() > 131072
        {
            continue;
        }
        if let Ok(job) = parse(path.clone(), std::fs::read(&path)?) {
            jobs.push(job);
        }
    }
    Ok(jobs)
}

async fn systemctl(args: &[&str]) -> Result<String> {
    let mut command = tokio::process::Command::new("systemctl");
    command
        .args(["--user", "--no-pager"])
        .args(args)
        .kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(5), command.output())
        .await
        .context("systemd user service command timed out")?
        .context("Linux reconnect requires systemctl and a running systemd user manager")?;
    if !output.status.success() {
        bail!("systemd user service unavailable; check systemctl --user status");
    }
    Ok(String::from_utf8(output.stdout)?)
}

// Resource-only overrides cannot change executable, endpoint, login or permissions.
// systemctl set-property commonly stores these separately under user.control.
fn resource_override(path: &Path) -> Result<()> {
    let meta = path.symlink_metadata()?;
    if !path.is_absolute()
        || !meta.is_file()
        || meta.mode() & 0o022 != 0
        || ![0, unsafe { libc::getuid() }].contains(&meta.uid())
        || meta.len() > 131072
    {
        bail!("Unsafe systemd resource override");
    }
    let text = std::fs::read_to_string(path)?;
    let mut service = false;
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with(['#', ';']) {
            continue;
        }
        if line == "[Service]" {
            service = true;
            continue;
        }
        let (key, value) = line
            .split_once('=')
            .context("Unsupported systemd override")?;
        if !service
            || !matches!(
                key.trim(),
                "MemoryHigh" | "MemoryMax" | "TasksMax" | "CPUQuota"
            )
            || value.contains(['\\', '%', '$'])
        {
            bail!("Reconnect supports only resource-limit drop-ins");
        }
    }
    Ok(())
}

fn loaded_pid(job: &Job, output: &str) -> Result<u32> {
    let props: BTreeMap<_, _> = output.lines().filter_map(|s| s.split_once('=')).collect();
    for (key, value) in [
        ("Id", job.label.as_str()),
        ("LoadState", "loaded"),
        ("ActiveState", "active"),
        ("SubState", "running"),
        ("NeedDaemonReload", "no"),
        (
            "FragmentPath",
            job.path.to_str().context("Invalid unit path")?,
        ),
    ] {
        if props.get(key) != Some(&value) {
            bail!("Loaded systemd service differs from preview or is unavailable ({key})");
        }
    }
    let overrides = props
        .get("DropInPaths")
        .context("Loaded drop-in inventory unavailable")?;
    for path in overrides.split_whitespace() {
        resource_override(Path::new(path))?;
    }
    if !matches!(props.get("Type"), Some(&"simple" | &"exec")) {
        bail!("Reconnect requires a simple or exec systemd service");
    }
    props
        .get("MainPID")
        .and_then(|p| p.parse::<u32>().ok())
        .filter(|p| *p > 0)
        .context("Service PID unavailable")
}

fn process_matches(job: &Job, proc_dir: &Path) -> Result<()> {
    let meta = proc_dir.metadata()?;
    if meta.uid() != unsafe { libc::getuid() } {
        bail!("Service process belongs to another user");
    }
    let bytes = std::fs::read(proc_dir.join("cmdline"))?;
    let args: Vec<_> = bytes
        .strip_suffix(&[0])
        .unwrap_or(&bytes)
        .split(|b| *b == 0)
        .map(std::str::from_utf8)
        .collect::<std::result::Result<_, _>>()?;
    if args != job.args {
        bail!("Loaded service arguments differ from saved configuration");
    }
    let bytes = std::fs::read(proc_dir.join("environ"))?;
    let env: BTreeMap<_, _> = bytes
        .split(|b| *b == 0)
        .filter_map(|b| std::str::from_utf8(b).ok())
        .filter_map(|s| s.split_once('='))
        .collect();
    for (k, v) in &job.env {
        if env.get(k.as_str()) != Some(&v.as_str()) {
            bail!("Loaded service environment differs from saved configuration");
        }
    }
    // The account probe must use the same login directory as the running owner.
    let expected_home = job
        .env
        .get("HOME")
        .cloned()
        .or_else(|| std::env::var("HOME").ok())
        .context("Dashboard HOME unavailable")?;
    if env.get("HOME").copied() != Some(expected_home.as_str()) {
        bail!("Service login environment differs; run the dashboard with its HOME");
    }
    let default_codex = PathBuf::from(&expected_home).join(".codex");
    let expected_codex = job
        .env
        .get("CODEX_HOME")
        .cloned()
        .or_else(|| std::env::var("CODEX_HOME").ok())
        .map(PathBuf::from)
        .unwrap_or_else(|| default_codex.clone());
    let actual_codex = env
        .get("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or(default_codex);
    if expected_codex != actual_codex
        && expected_codex.canonicalize()? != actual_codex.canonicalize()?
    {
        bail!("Service login environment differs; run the dashboard with its CODEX_HOME");
    }
    if let Some(cwd) = &job.cwd
        && std::fs::read_link(proc_dir.join("cwd"))?.canonicalize()?
            != Path::new(cwd).canonicalize()?
    {
        bail!("Service working directory differs from saved configuration");
    }
    Ok(())
}

pub(super) async fn pid(job: &Job) -> Result<u32> {
    if std::fs::read(&job.path)? != job.raw {
        bail!("Saved service changed");
    }
    let output = systemctl(&["show", "--property=Id,LoadState,ActiveState,SubState,NeedDaemonReload,DropInPaths,FragmentPath,Type,MainPID", "--", &job.label]).await?;
    let pid = loaded_pid(job, &output)?;
    process_matches(job, &PathBuf::from(format!("/proc/{pid}")))?;
    Ok(pid)
}

pub(super) async fn restart(job: &Job) -> Result<()> {
    systemctl(&["--no-block", "restart", "--", &job.label]).await?;
    Ok(())
}

// Replace only ExecStart; preserve environment, resource limits and installation policy.
pub(super) fn rewrite(job: &Job, args: &[String]) -> Result<Vec<u8>> {
    let mut quoted = Vec::new();
    for arg in args {
        if arg
            .chars()
            .any(|c| c.is_control() || ['\\', '$', '%'].contains(&c))
        {
            bail!("Permission arguments require literal systemd values");
        }
        // A single quote can be represented in its own double-quoted segment.
        quoted.push(format!("'{}'", arg.replace('\'', "'\"'\"'")));
    }
    let mut service = false;
    let mut count = 0;
    let mut output = String::new();
    for line in std::str::from_utf8(&job.raw)?.split_inclusive('\n') {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            service = trimmed == "[Service]";
        }
        if service
            && trimmed
                .split_once('=')
                .is_some_and(|(key, _)| key.trim() == "ExecStart")
        {
            count += 1;
            output.push_str(&format!("ExecStart={}\n", quoted.join(" ")));
        } else {
            output.push_str(line);
        }
    }
    let parsed = parse(job.path.clone(), output.as_bytes().to_vec())?;
    if count != 1 || parsed.args != args || parsed.env != job.env || parsed.cwd != job.cwd {
        bail!("Rewritten unit did not preserve service configuration");
    }
    Ok(output.into_bytes())
}

pub(super) async fn reload() -> Result<()> {
    systemctl(&["daemon-reload"]).await?;
    Ok(())
}

pub(super) async fn stop(job: &Job) -> Result<()> {
    systemctl(&["--no-block", "stop", "--", &job.label]).await?;
    tokio::time::timeout(Duration::from_secs(35), async {
        loop {
            let state =
                systemctl(&["show", "--property=ActiveState,MainPID", "--", &job.label]).await?;
            if state
                .lines()
                .any(|s| matches!(s, "ActiveState=inactive" | "ActiveState=failed"))
                && state.lines().any(|s| s == "MainPID=0")
            {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .context("Service did not stop; configuration was not applied")?
}

pub(super) async fn start(job: &Job) -> Result<()> {
    systemctl(&["--no-block", "start", "--", &job.label]).await?;
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            if pid(job).await.is_ok() {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .context("Started service did not match saved configuration")?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    fn unit(extra: &str) -> Vec<u8> {
        format!("[Unit]\nDescription=Reconnect fixture\n[Service]\nType=exec\nExecStart=/usr/bin/codex app-server --listen ws://127.0.0.1:4500\n{extra}\n[Install]\nWantedBy=default.target\n").into_bytes()
    }
    fn job() -> Job {
        parse(PathBuf::from("/tmp/owner.service"), unit("")).unwrap()
    }
    fn properties(job: &Job, pid: u32) -> String {
        format!(
            "Id={}\nLoadState=loaded\nActiveState=active\nSubState=running\nNeedDaemonReload=no\nDropInPaths=\nFragmentPath={}\nType=exec\nMainPID={pid}\n",
            job.label,
            job.path.display()
        )
    }

    #[test]
    fn permission_rewrite_preserves_unit_settings_and_literal_arguments() {
        let original = parse(
            PathBuf::from("/tmp/owner.service"),
            unit("Environment=EXAMPLE=literal\nMemoryMax=4G\nUMask=0077"),
        )
        .unwrap();
        for policy in ["full", "read-only", "workspace-network"] {
            let mut args = super::super::policy_args(&original.args, policy, false).unwrap();
            args.extend(["-c".into(), "model=\"example's model\"".into()]);
            let raw = rewrite(&original, &args).unwrap();
            let next = parse(original.path.clone(), raw).unwrap();
            assert_eq!(next.args, args);
            assert_eq!(next.env, original.env);
            let text = String::from_utf8(next.raw).unwrap();
            assert!(text.contains("MemoryMax=4G\nUMask=0077"));
            assert!(text.contains("[Install]\nWantedBy=default.target"));
        }
        for value in ["$HOME", "%h", "line\nbreak", "back\\slash"] {
            assert!(rewrite(&original, &["/bin/codex".into(), value.into()]).is_err());
        }
    }
    #[test]
    fn parses_direct_owner_and_resident_units() {
        let owner = parse(PathBuf::from("/tmp/owner.service"), unit("Environment=\"CODEX_HOME=/tmp/codex home\" HOME=/tmp\nWorkingDirectory=/tmp\nRestart=always\nUMask=0077\nMemoryMax=4G\nTasksMax=2048")).unwrap();
        assert_eq!(
            super::super::option(&owner.args, "--listen").as_deref(),
            Some("ws://127.0.0.1:4500")
        );
        assert_eq!(owner.env["CODEX_HOME"], "/tmp/codex home");
        assert_eq!(owner.cwd.as_deref(), Some("/tmp"));
        let raw = b"[Service]\nExecStart=/opt/bin/codex-monitor resident --endpoint ws://127.0.0.1:4500 --thread one --thread two\n".to_vec();
        let resident = parse(PathBuf::from("/tmp/resident.service"), raw).unwrap();
        assert_eq!(super::super::configured(&[resident]).len(), 2);
    }

    #[test]
    fn rejects_ambiguous_or_indirect_service_configuration() {
        for directive in [
            "ExecStart=/bin/other",
            "EnvironmentFile=/tmp/env",
            "Type=forking",
            "RootDirectory=/tmp",
            "ExecStartPre=/bin/true",
            "PassEnvironment=CODEX_HOME",
            "Environment=CODEX_HOME=%h/.codex",
            "WorkingDirectory=-/tmp",
        ] {
            assert!(
                parse(PathBuf::from("/tmp/owner.service"), unit(directive)).is_err(),
                "{directive}"
            );
        }
        assert!(parse(PathBuf::from("/tmp/owner@one.service"), unit("")).is_err());
        for value in [
            "/bin/codex $ARGS",
            "/bin/codex %h",
            "\"unterminated",
            "foo\\x20bar",
        ] {
            assert!(words(value).is_err(), "{value}");
        }
        assert_eq!(
            words("-c sandbox_mode=\"workspace-write\"").unwrap(),
            vec!["-c", "sandbox_mode=workspace-write"]
        );
        assert_eq!(
            words("/bin/codex -c 'model=\"example\"' \"\"").unwrap(),
            vec!["/bin/codex", "-c", "model=\"example\"", ""]
        );
    }

    #[test]
    fn discovery_excludes_symlinks_and_writable_units() {
        let dir = tempfile::tempdir().unwrap();
        let good = dir.path().join("owner.service");
        std::fs::write(&good, unit("")).unwrap();
        std::fs::set_permissions(&good, std::fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&good, dir.path().join("alias.service")).unwrap();
        let bad = dir.path().join("writable.service");
        std::fs::write(&bad, unit("")).unwrap();
        std::fs::set_permissions(&bad, std::fs::Permissions::from_mode(0o666)).unwrap();
        assert_eq!(read_jobs(dir.path()).unwrap().len(), 1);
        assert!(read_jobs(&dir.path().join("missing")).unwrap().is_empty());
    }

    #[test]
    fn validates_loaded_identity_and_rejects_stale_or_overridden_units() {
        let job = job();
        let output = properties(&job, 42);
        assert_eq!(loaded_pid(&job, &output).unwrap(), 42);
        for (from, to) in [
            ("Id=owner.service", "Id=other.service"),
            ("LoadState=loaded", "LoadState=not-found"),
            ("ActiveState=active", "ActiveState=inactive"),
            ("SubState=running", "SubState=dead"),
            ("NeedDaemonReload=no", "NeedDaemonReload=yes"),
            ("DropInPaths=", "DropInPaths=/tmp/override.conf"),
            (
                "FragmentPath=/tmp/owner.service",
                "FragmentPath=/etc/owner.service",
            ),
            ("Type=exec", "Type=forking"),
            ("MainPID=42", "MainPID=0"),
        ] {
            assert!(loaded_pid(&job, &output.replace(from, to)).is_err(), "{to}");
        }
        assert!(loaded_pid(&job, "MainPID=42").is_err());
    }

    #[test]
    fn permits_only_safe_resource_drop_ins() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("50-MemoryMax.conf");
        std::fs::write(&path, "[Service]\nMemoryMax=8G\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let output = properties(&job(), 42)
            .replace("DropInPaths=", &format!("DropInPaths={}", path.display()));
        assert_eq!(loaded_pid(&job(), &output).unwrap(), 42);
        for content in [
            "[Service]\nExecStart=/bin/other\n",
            "[Service]\nEnvironment=CODEX_HOME=/tmp\n",
            "[Unit]\nRequires=other.service\n",
        ] {
            std::fs::write(&path, content).unwrap();
            assert!(loaded_pid(&job(), &output).is_err());
        }
        std::fs::write(&path, "[Service]\nMemoryMax=8G\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)).unwrap();
        assert!(loaded_pid(&job(), &output).is_err());
    }

    #[test]
    fn validates_running_arguments_environment_and_working_directory() {
        let dir = tempfile::tempdir().unwrap();
        let mut job = job();
        job.cwd = Some(dir.path().to_str().unwrap().into());
        job.env.insert("FIXTURE".into(), "expected".into());
        let cmdline = format!("{}\0", job.args.join("\0"));
        std::fs::write(dir.path().join("cmdline"), &cmdline).unwrap();
        let mut env = String::from("FIXTURE=expected\0");
        for key in ["HOME", "CODEX_HOME"] {
            if let Ok(v) = std::env::var(key) {
                env.push_str(&format!("{key}={v}\0"));
            }
        }
        std::fs::write(dir.path().join("environ"), &env).unwrap();
        symlink(dir.path(), dir.path().join("cwd")).unwrap();
        process_matches(&job, dir.path()).unwrap();
        std::fs::write(dir.path().join("cmdline"), b"/bin/other\0").unwrap();
        assert!(process_matches(&job, dir.path()).is_err());
        std::fs::write(dir.path().join("cmdline"), &cmdline).unwrap();
        std::fs::write(
            dir.path().join("environ"),
            env.replace("expected", "changed"),
        )
        .unwrap();
        assert!(process_matches(&job, dir.path()).is_err());
        std::fs::write(dir.path().join("environ"), &env).unwrap();
        job.cwd = Some("/".into());
        assert!(process_matches(&job, dir.path()).is_err());
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    #[ignore = "read-only check of explicitly selected local services; requires CODEX_MONITOR_TEST_ENDPOINT"]
    async fn configured_user_services_match_running_processes() {
        let endpoint = std::env::var("CODEX_MONITOR_TEST_ENDPOINT").unwrap();
        let (owner, residents) = super::super::configuration(&endpoint).unwrap();
        pid(&owner).await.unwrap();
        for resident in residents {
            pid(&resident).await.unwrap();
        }
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    #[ignore = "requires a running systemd user manager; creates only a disposable sleep service"]
    async fn real_user_manager_restarts_only_disposable_service() {
        let dir = PathBuf::from(std::env::var("XDG_RUNTIME_DIR").unwrap()).join("systemd/user");
        std::fs::create_dir_all(&dir).unwrap();
        let label = format!(
            "codex-monitor-reconnect-test-{}.service",
            uuid::Uuid::new_v4().simple()
        );
        let path = dir.join(&label);
        let mut raw = String::from("[Service]\nType=exec\nExecStart=/usr/bin/sleep 120\n");
        for key in ["HOME", "CODEX_HOME"] {
            if let Ok(v) = std::env::var(key) {
                raw.push_str(&format!("Environment=\"{key}={v}\"\n"));
            }
        }
        std::fs::write(&path, &raw).unwrap();
        let job = parse(path.clone(), raw.into_bytes()).unwrap();
        let result: Result<()> = async {
            systemctl(&["daemon-reload"]).await?;
            systemctl(&["start", "--", &label]).await?;
            let before = pid(&job).await?;
            restart(&job).await?;
            for _ in 0..40 {
                tokio::time::sleep(Duration::from_millis(100)).await;
                if pid(&job).await.is_ok_and(|after| after != before) {
                    stop(&job).await?;
                    let args = vec!["/usr/bin/sleep".into(), "121".into()];
                    let raw = rewrite(&job, &args)?;
                    let changed = parse(path.clone(), raw.clone())?;
                    std::fs::write(&path, raw)?;
                    reload().await?;
                    start(&changed).await?;
                    pid(&changed).await?;
                    assert!(pid(&job).await.is_err());
                    stop(&changed).await?;
                    std::fs::write(&path, &job.raw)?;
                    reload().await?;
                    start(&job).await?;
                    pid(&job).await?;
                    return Ok(());
                }
            }
            bail!("Disposable service did not acquire a new verified PID");
        }
        .await;
        let _ = systemctl(&["stop", "--", &label]).await;
        let _ = std::fs::remove_file(&path);
        let _ = systemctl(&["daemon-reload"]).await;
        let _ = systemctl(&["reset-failed", "--", &label]).await;
        result.unwrap();
    }
}
