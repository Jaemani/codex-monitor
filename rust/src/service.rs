//! Native executable installation and receiver supervision.
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
fn home() -> Result<PathBuf> {
    Ok(PathBuf::from(std::env::var("HOME")?))
}
fn atomic_link(target: &Path, path: &Path) -> Result<()> {
    let tmp = path.with_extension(format!("link-{}", uuid::Uuid::new_v4()));
    symlink(target, &tmp)?;
    std::fs::rename(tmp, path)?;
    Ok(())
}
pub fn update_available() -> bool {
    std::env::current_exe().is_ok_and(|exe| installed_update(&exe))
}
fn installed_update(exe: &Path) -> bool {
    let Some(releases) = exe.ancestors().nth(3) else {
        return false;
    };
    let Some(prefix) = releases.parent() else {
        return false;
    };
    releases.file_name().is_some_and(|s| s == "releases")
        && prefix.join("native-install.json").is_file()
        && prefix
            .join("current/bin/codex-monitor")
            .canonicalize()
            .is_ok_and(|current| current != exe)
}

pub fn install(prefix: Option<&Path>, bin_dir: Option<&Path>, adopt: bool) -> Result<Value> {
    let default = home()?.join(".local/share/codex-monitor-rust");
    let prefix = prefix.unwrap_or(&default);
    let default_bin = home()?.join(".local/bin");
    let bin_dir = bin_dir.unwrap_or(&default_bin);
    if !prefix.is_absolute() || !bin_dir.is_absolute() {
        bail!("Install paths must be absolute");
    }
    if prefix.exists()
        && !prefix.join("native-install.json").exists()
        && std::fs::read_dir(prefix)?.next().is_some()
    {
        bail!("Nonempty prefix is not owned by the native installer; select a new prefix");
    }
    let source = std::env::current_exe()?;
    let bytes = std::fs::read(&source)?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    let target = bin_dir.join("codex-monitor");
    let previous = if target.symlink_metadata().is_ok() {
        if !target.is_symlink() {
            bail!("Existing command is not an owned symlink");
        }
        let link = std::fs::read_link(&target)?;
        if link != prefix.join("current/bin/codex-monitor")
            && !(adopt && link == home()?.join(".local/share/codex-monitor/bin/codex-monitor"))
        {
            bail!("Existing command is foreign; explicit legacy adoption is required");
        }
        Some(link)
    } else {
        None
    };
    let legacy_launcher = home()?.join(".local/share/codex-monitor/bin/codex-monitor");
    let legacy_previous = if adopt && legacy_launcher.symlink_metadata().is_ok() {
        let marker: Value = serde_json::from_slice(&std::fs::read(
            home()?.join(".local/share/codex-monitor/.codex-monitor-installer.json"),
        )?)?;
        if marker["owner"] != "codex-monitor-local-installer" || !legacy_launcher.is_symlink() {
            bail!("Legacy launcher ownership is unverified");
        }
        Some(std::fs::read_link(&legacy_launcher)?)
    } else {
        None
    };
    std::fs::create_dir_all(prefix)?;
    std::fs::create_dir_all(bin_dir)?;
    let _lock = crate::lock(prefix, "install.lock")?;
    let release =
        prefix
            .join("releases")
            .join(format!("{}-{}", env!("CARGO_PKG_VERSION"), &digest[..16]));
    std::fs::create_dir_all(release.join("bin"))?;
    let executable = release.join("bin/codex-monitor");
    if executable.exists() {
        if std::fs::read(&executable)? != bytes {
            bail!("Existing release digest mismatch");
        }
    } else {
        crate::private_write(&executable, &bytes)?;
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755))?;
    }
    let manifest = json!({"runtime":"rust","version":env!("CARGO_PKG_VERSION"),"sha256":digest,"previous_command":previous,"legacy_launcher_previous":legacy_previous,"release":release});
    crate::private_write(
        &release.join("manifest.json"),
        &serde_json::to_vec_pretty(&manifest)?,
    )?;
    atomic_link(&release, &prefix.join("current"))?;
    atomic_link(&prefix.join("current/bin/codex-monitor"), &target)?;
    if legacy_previous.is_some() {
        atomic_link(&prefix.join("current/bin/codex-monitor"), &legacy_launcher)?;
    }
    crate::private_write(
        &prefix.join("native-install.json"),
        &serde_json::to_vec_pretty(&manifest)?,
    )?;
    Ok(
        json!({"runtime":"rust","command":target,"release":release,"state_changed":false,"previous_command":previous}),
    )
}
fn label(root: &Path) -> Result<String> {
    Ok(format!(
        "com.codex.monitor.{}",
        &format!(
            "{:x}",
            Sha256::digest(root.canonicalize()?.to_string_lossy().as_bytes())
        )[..16]
    ))
}
async fn run(program: &str, args: &[&str]) -> Result<bool> {
    let mut cmd = tokio::process::Command::new(program);
    cmd.args(args).kill_on_drop(true);
    Ok(tokio::time::timeout(Duration::from_secs(10), cmd.output())
        .await??
        .status
        .success())
}
pub async fn action(root: &Path, action: &str) -> Result<Value> {
    if !cfg!(target_os = "macos") {
        bail!(
            "Native service supervision currently supports macOS; run serve under your Linux supervisor"
        );
    }
    let label = label(root)?;
    let domain = format!("gui/{}", unsafe { libc::getuid() });
    let target = format!("{domain}/{label}");
    let path = home()?
        .join("Library/LaunchAgents")
        .join(format!("{label}.plist"));
    let loaded = run("/bin/launchctl", &["print", &target]).await?;
    let existing = if path.exists() {
        Some(plist::from_bytes::<Value>(&std::fs::read(&path)?)?)
    } else {
        None
    };
    if let Some(v) = &existing {
        let args = v["ProgramArguments"]
            .as_array()
            .context("Invalid saved receiver service")?;
        if v["Label"] != label
            || !args.windows(2).any(|w| {
                w[0] == "--state"
                    && w[1].as_str().is_some_and(|p| {
                        Path::new(p).canonicalize().ok() == root.canonicalize().ok()
                    })
            })
            || !args.iter().any(|v| v == "serve")
        {
            bail!("Refusing foreign receiver service");
        }
    }
    match action {
        "status" => {}
        "stop" => {
            if loaded && !run("/bin/launchctl", &["bootout", &target]).await? {
                bail!("Receiver stop failed");
            }
        }
        "uninstall" => {
            if loaded && !run("/bin/launchctl", &["bootout", &target]).await? {
                bail!("Receiver stop failed");
            }
            if existing.is_some() {
                std::fs::remove_file(&path)?;
            }
        }
        "install" => {
            if loaded {
                bail!("Stop the receiver before changing its executable");
            }
            let executable = std::env::current_exe()?.canonicalize()?;
            let logs = root.join("service");
            std::fs::create_dir_all(&logs)?;
            std::fs::create_dir_all(path.parent().unwrap())?;
            let mut env = serde_json::Map::new();
            for key in [
                "HOME",
                "PATH",
                "CODEX_HOME",
                "CODEX_MONITOR_SERVER_TOKEN_FILE",
            ] {
                if let Ok(v) = std::env::var(key) {
                    env.insert(key.into(), json!(v));
                }
            }
            // Preserve the prior receiver environment, including its explicit Codex home.
            if let Some(v) = &existing
                && let Some(previous) = v["EnvironmentVariables"].as_object()
            {
                for (k, v) in previous {
                    env.insert(k.clone(), v.clone());
                }
            }
            let spec = json!({"Label":label,"ProgramArguments":[executable,"--state",root,"serve"],"EnvironmentVariables":env,"WorkingDirectory":root,"RunAtLoad":true,"KeepAlive":true,"StandardOutPath":logs.join("stdout.log"),"StandardErrorPath":logs.join("stderr.log")});
            let mut raw = Vec::new();
            plist::to_writer_xml(&mut raw, &spec)?;
            crate::private_write(&path, &raw)?;
            if !run(
                "/bin/launchctl",
                &[
                    "bootstrap",
                    &domain,
                    path.to_str().context("Invalid service path")?,
                ],
            )
            .await?
            {
                bail!("Receiver bootstrap failed; saved configuration retained");
            }
        }
        "restart" => {
            if loaded && !run("/bin/launchctl", &["bootout", &target]).await? {
                bail!("Receiver stop failed");
            }
            if !run(
                "/bin/launchctl",
                &[
                    "bootstrap",
                    &domain,
                    path.to_str().context("Invalid service path")?,
                ],
            )
            .await?
            {
                bail!("Receiver restart failed");
            }
        }
        "start" => {
            if !loaded
                && !run(
                    "/bin/launchctl",
                    &[
                        "bootstrap",
                        &domain,
                        path.to_str().context("Invalid service path")?,
                    ],
                )
                .await?
            {
                bail!("Receiver start failed");
            }
        }
        _ => bail!("Unsupported service action"),
    }
    Ok(
        json!({"runtime":"rust","label":label,"plist":path,"installed":path.exists(),"loaded":run("/bin/launchctl",&["print",&target]).await?}),
    )
}

pub fn install_skill() -> Result<Value> {
    let target = std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or(home()?.join(".codex"))
        .join("skills/codex-monitor");
    if target.is_symlink() {
        bail!("Skill directory is a symlink; update its source explicitly");
    }
    std::fs::create_dir_all(&target)?;
    let path = target.join("SKILL.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/SKILL.md"),
    )?;
    let path = target.join("agents/openai.yaml");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/agents/openai.yaml"),
    )?;
    let path = target.join("references/conditions.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/references/conditions.md"),
    )?;
    let path = target.join("references/installation.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!(
            "../../plugins/codex-monitor/skills/codex-monitor/references/installation.md"
        ),
    )?;
    let path = target.join("references/operations.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/references/operations.md"),
    )?;
    let path = target.join("references/requests.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/references/requests.md"),
    )?;
    let path = target.join("references/resident.md");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/references/resident.md"),
    )?;
    let path = target.join("scripts/monitor.py");
    std::fs::create_dir_all(path.parent().unwrap())?;
    crate::private_write(
        &path,
        include_bytes!("../../plugins/codex-monitor/skills/codex-monitor/scripts/monitor.py"),
    )?;
    Ok(json!({"path":target,"runtime":"rust"}))
}

#[cfg(test)]
mod update_tests {
    use super::*;
    #[test]
    fn only_owned_installations_report_replaced_executables() {
        let dir = tempfile::tempdir().unwrap();
        let prefix = dir.path();
        for version in ["old", "new"] {
            std::fs::create_dir_all(prefix.join(format!("releases/{version}/bin"))).unwrap();
            std::fs::write(
                prefix.join(format!("releases/{version}/bin/codex-monitor")),
                version,
            )
            .unwrap();
        }
        std::fs::write(prefix.join("native-install.json"), "{}").unwrap();
        let old = prefix.join("releases/old/bin/codex-monitor");
        atomic_link(&prefix.join("releases/old"), &prefix.join("current")).unwrap();
        assert!(!installed_update(&old));
        atomic_link(&prefix.join("releases/new"), &prefix.join("current")).unwrap();
        assert!(installed_update(&old));
        assert!(!installed_update(
            &prefix.join("releases/new/bin/codex-monitor")
        ));
        std::fs::remove_file(prefix.join("native-install.json")).unwrap();
        assert!(!installed_update(&old));
    }
}
