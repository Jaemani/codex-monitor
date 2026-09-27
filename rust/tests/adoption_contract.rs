use codex_monitor_rs::store::Store;
use rusqlite::Connection;
use serde_json::json;
use std::process::Command;
fn fixture() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    std::fs::write(
        root.join("config.json"),
        json!({"version":1,"port":8766,"sources":{},"limits":{}}).to_string(),
    )
    .unwrap();
    std::fs::write(root.join("admin.token"), "unchanged-test-credential").unwrap();
    drop(Store::open(&root.join("monitor.sqlite3")).unwrap());
    let db = Connection::open(root.join("monitor.sqlite3")).unwrap();
    db.execute_batch("CREATE TABLE decisions(id INTEGER PRIMARY KEY,delivery_id TEXT,action TEXT,reason TEXT,created REAL);
 ALTER TABLE managed_watches ADD COLUMN lifecycle_epoch INTEGER DEFAULT 0;
 ALTER TABLE managed_watches ADD COLUMN debounce_seconds REAL DEFAULT 0;
 ALTER TABLE managed_watches ADD COLUMN condition_json TEXT;
 INSERT INTO bindings VALUES('route','thread','ws://127.0.0.1:1','[\"source\"]',1,0);
 INSERT INTO events(id,binding,source,event_id,envelope,client_id,state,created,updated) VALUES('receipt','route','source','event','{}','native-client','uncertain',1,1);
 INSERT INTO managed_watches(id,thread,name,path,interval,endpoint,binding,created,updated) VALUES('watch','thread','file','/tmp/file',2,'ws://127.0.0.1:1','watch-route',1,1);
 INSERT INTO bindings VALUES('watch-route','thread','ws://127.0.0.1:1','[\"managed/file\"]',1,0);").unwrap();
    std::fs::create_dir(root.join("managed")).unwrap();
    std::fs::write(
        root.join("managed/watch.json"),
        json!({"last":{"path":"/tmp/file","state":"missing"},"pending":null}).to_string(),
    )
    .unwrap();
    dir
}
#[test]
fn migration_preserves_dedup_state_credentials_and_baselines() {
    let dir = fixture();
    let output = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .args(["--state", dir.path().to_str().unwrap(), "migrate"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let db = Connection::open(dir.path().join("rust.sqlite3")).unwrap();
    let row: (String, String, String) = db
        .query_row("SELECT id,client_id,state FROM events", [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .unwrap();
    assert_eq!(
        row,
        ("receipt".into(), "native-client".into(), "uncertain".into())
    );
    let checkpoint: String = db
        .query_row("SELECT checkpoint FROM managed_watches", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&checkpoint).unwrap(),
        json!({"path":"/tmp/file","state":"missing"})
    );
    assert_eq!(
        std::fs::read_to_string(dir.path().join("admin.token")).unwrap(),
        "unchanged-test-credential"
    );
    let rerun = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .args(["--state", dir.path().to_str().unwrap(), "migrate"])
        .output()
        .unwrap();
    assert!(!rerun.status.success());
}
#[test]
fn pending_checkpoint_blocks_without_adopting_state() {
    let dir = fixture();
    std::fs::write(
        dir.path().join("managed/watch.json"),
        json!({"last":{},"pending":{"id":"pending"}}).to_string(),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .args(["--state", dir.path().to_str().unwrap(), "migrate"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("rust.sqlite3").exists());
    assert!(
        !std::fs::read_to_string(dir.path().join("config.json"))
            .unwrap()
            .contains("rust")
    );
}
#[test]
fn running_receiver_blocks_migration() {
    use fs2::FileExt;
    let dir = fixture();
    let file = std::fs::File::create(dir.path().join("serve.lock")).unwrap();
    file.lock_exclusive().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .args(["--state", dir.path().to_str().unwrap(), "migrate"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("rust.sqlite3").exists());
}
#[test]
fn native_install_has_no_python_launcher() {
    let dir = tempfile::tempdir().unwrap();
    let prefix = dir.path().join("runtime");
    let bin = dir.path().join("bin");
    let out = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .args([
            "install",
            "--prefix",
            prefix.to_str().unwrap(),
            "--bin-dir",
            bin.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let source = std::fs::read(env!("CARGO_BIN_EXE_codex-monitor-rs")).unwrap();
    assert_eq!(source, std::fs::read(bin.join("codex-monitor")).unwrap());
    assert!(
        Command::new(bin.join("codex-monitor"))
            .arg("--version")
            .status()
            .unwrap()
            .success()
    );
}

#[test]
fn adoption_redirects_owned_legacy_entrypoint_without_deleting_old_release() {
    use std::os::unix::fs::symlink;
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path();
    let old = home.join(".local/share/codex-monitor");
    let bin = home.join(".local/bin");
    std::fs::create_dir_all(old.join("bin")).unwrap();
    std::fs::create_dir_all(&bin).unwrap();
    std::fs::write(old.join("historical-executable"), "legacy recovery copy").unwrap();
    std::fs::write(
        old.join(".codex-monitor-installer.json"),
        json!({"owner":"codex-monitor-local-installer"}).to_string(),
    )
    .unwrap();
    symlink(
        old.join("historical-executable"),
        old.join("bin/codex-monitor"),
    )
    .unwrap();
    symlink(old.join("bin/codex-monitor"), bin.join("codex-monitor")).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_codex-monitor-rs"))
        .env("HOME", home)
        .args(["install", "--adopt-python"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        old.join("bin/codex-monitor").canonicalize().unwrap(),
        bin.join("codex-monitor").canonicalize().unwrap()
    );
    assert_eq!(
        std::fs::read_to_string(old.join("historical-executable")).unwrap(),
        "legacy recovery copy"
    );
}
