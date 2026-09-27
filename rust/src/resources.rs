//! Bounded read-only observations; never attribute shared owners to projects.
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
struct Cache {
    at: Instant,
    root: std::path::PathBuf,
    pid: u64,
    value: Value,
}
static CACHE: OnceLock<Mutex<Option<Cache>>> = OnceLock::new();
pub fn process_tree(output: &str, pid: u64) -> Value {
    let mut rows = BTreeMap::new();
    for line in output.lines() {
        let v: Vec<_> = line.split_whitespace().collect();
        if v.len() != 5 {
            continue;
        }
        if let (Ok(id), Ok(parent), Ok(rss), Ok(cpu)) = (
            v[0].parse::<u64>(),
            v[1].parse::<u64>(),
            v[3].parse::<u64>(),
            v[4].parse::<f64>(),
        ) {
            rows.insert(id, (parent, v[2].contains('Z'), rss * 1024, cpu));
        }
    }
    if !rows.contains_key(&pid) {
        return json!({"available":false,"reason":"receiver absent from process snapshot"});
    }
    let mut owned = BTreeSet::from([pid]);
    loop {
        let add: Vec<_> = rows
            .iter()
            .filter(|(id, r)| owned.contains(&r.0) && !owned.contains(id))
            .map(|(id, _)| *id)
            .collect();
        if add.is_empty() {
            break;
        }
        owned.extend(add);
    }
    json!({"available":true,"processes":owned.len(),"children":owned.len()-1,"rss_bytes":owned.iter().map(|p|rows[p].2).sum::<u64>(),"cpu_percent":owned.iter().map(|p|rows[p].3).sum::<f64>(),"zombies":owned.iter().filter(|p|rows[p].1).count(),"scope":"receiver and attached descendants; excludes owners, dashboards and detached tools"})
}
fn storage(root: &Path) -> Value {
    let start = Instant::now();
    let mut paths = vec![root.to_path_buf()];
    let mut count = 0;
    let (mut db, mut logs, mut other) = (0u64, 0u64, 0u64);
    while let Some(path) = paths.pop() {
        let Ok(entries) = std::fs::read_dir(path) else {
            return json!({"available":false,"reason":"storage scan unavailable"});
        };
        for entry in entries {
            count += 1;
            if count > 10000 || start.elapsed() > Duration::from_millis(100) {
                return json!({"available":false,"reason":"storage scan budget exceeded"});
            }
            let Ok(entry) = entry else { continue };
            let Ok(meta) = entry.path().symlink_metadata() else {
                continue;
            };
            if meta.is_symlink() {
                continue;
            }
            if meta.is_dir() {
                paths.push(entry.path());
            } else if meta.is_file() {
                let name = entry.file_name().to_string_lossy().to_lowercase();
                if name.contains(".sqlite") || name.contains(".db") {
                    db += meta.len();
                } else if name.contains(".log") {
                    logs += meta.len();
                } else {
                    other += meta.len();
                }
            }
        }
    }
    json!({"available":true,"database_bytes":db,"log_bytes":logs,"other_bytes":other,"total_bytes":db+logs+other,"scope":"state logical bytes; excludes runtime and watched files"})
}
pub async fn sample(root: &Path, receiver: &Value) -> Value {
    let pid = receiver["pid"].as_u64().unwrap_or(0);
    let now = Instant::now();
    if let Ok(cache) = CACHE.get_or_init(|| Mutex::new(None)).lock()
        && let Some(c) = cache.as_ref()
        && c.root == root
        && c.pid == pid
        && now.duration_since(c.at) < Duration::from_secs(30)
    {
        let mut v = c.value.clone();
        v["age_seconds"] = json!(now.duration_since(c.at).as_secs_f64());
        return v;
    }
    let mut cmd = tokio::process::Command::new("ps");
    cmd.args(["-axo", "pid=,ppid=,stat=,rss=,%cpu="])
        .env("LC_ALL", "C")
        .kill_on_drop(true);
    let process = if pid == 0 {
        json!({"available":false,"reason":"receiver identity unavailable"})
    } else {
        match tokio::time::timeout(Duration::from_secs(1), cmd.output()).await {
            Ok(Ok(out)) if out.status.success() => {
                process_tree(&String::from_utf8_lossy(&out.stdout), pid)
            }
            _ => json!({"available":false,"reason":"process sample unavailable"}),
        }
    };
    let mut value = json!({"process":process,"storage":storage(root),"sampled_at":crate::now(),"interval_seconds":30,"age_seconds":0,"projects":{}});
    if let Ok(mut cache) = CACHE.get_or_init(|| Mutex::new(None)).lock() {
        if let Some(old) = cache.as_ref()
            && old.root == root
        {
            let elapsed = now.duration_since(old.at).as_secs_f64();
            if let (Some(a), Some(b)) = (
                old.value["storage"]["total_bytes"].as_f64(),
                value["storage"]["total_bytes"].as_f64(),
            ) && elapsed > 0.0
            {
                value["storage"]["change_bytes_per_hour"] = json!((b - a) * 3600.0 / elapsed);
                value["storage"]["change_window_seconds"] = json!(elapsed);
            }
            if old.pid == pid && value["process"]["zombies"].as_u64().unwrap_or(0) > 0 {
                value["process"]["consecutive_zombie_samples"] = json!(
                    old.value["process"]["consecutive_zombie_samples"]
                        .as_u64()
                        .unwrap_or(0)
                        + 1
                );
            }
        }
        *cache = Some(Cache {
            at: now,
            root: root.to_owned(),
            pid,
            value: value.clone(),
        });
    }
    value
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counts_only_attached_tree() {
        let v = process_tree("1 0 S 100 1.0\n2 1 Z 10 0.0\n3 8 S 900 90.0", 1);
        assert_eq!(v["processes"], 2);
        assert_eq!(v["zombies"], 1);
        assert_eq!(v["rss_bytes"], 112640);
    }
}
