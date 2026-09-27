//! Discord transport, isolated from the receiver and from model execution.
//! Existing adapter SQLite receipts are retained during native adoption.
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use futures_util::{SinkExt, StreamExt};
use reqwest::{Client, Method};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::time::{Instant, timeout};
use tokio_tungstenite::tungstenite::Message;

const API: &str = "https://discord.com/api/v10";
const MAX_IMAGE: usize = 8 * 1024 * 1024;
fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn text<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or("")
}
fn numeric(s: &str) -> bool {
    (17..=20).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}
fn expand(p: &str) -> PathBuf {
    p.strip_prefix("~/")
        .map(|p| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(p))
        .unwrap_or_else(|| p.into())
}
fn write_private(path: &Path, data: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let temp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    f.write_all(data)?;
    f.sync_all()?;
    fs::rename(&temp, path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}
fn secret(path: &Path) -> Result<String> {
    use std::os::unix::fs::PermissionsExt;
    ensure!(
        fs::metadata(path)?.permissions().mode() & 0o077 == 0,
        "credential file must be private"
    );
    let value = fs::read_to_string(path)?.trim().to_string();
    ensure!(
        !value.is_empty() && !value.chars().any(char::is_whitespace),
        "invalid credential file"
    );
    Ok(value)
}
fn validate(c: &Value, source: &str) -> Result<()> {
    ensure!(
        regex::Regex::new(r"^[a-z][a-z0-9-]{0,63}$")?.is_match(source),
        "invalid source"
    );
    ensure!(
        regex::Regex::new(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")?.is_match(text(c, "project_name")),
        "invalid project name"
    );
    for k in ["guild_id", "channel_id", "application_id"] {
        ensure!(numeric(text(c, k)), "invalid Discord ID");
    }
    let users = c["allowed_user_ids"]
        .as_array()
        .context("allowed_user_ids required")?;
    ensure!(
        !users.is_empty() && users.iter().all(|v| v.as_str().is_some_and(numeric)),
        "explicit allowed user IDs required"
    );
    let roles = c["roles"].as_object().context("roles required")?;
    let threads = c["discord_threads"]
        .as_object()
        .context("discord_threads required")?;
    for (role, thread) in roles {
        ensure!(
            !role.is_empty() && role.bytes().all(|b| b.is_ascii_lowercase()),
            "invalid role"
        );
        ensure!(
            thread.is_null()
                || thread
                    .as_str()
                    .is_some_and(|s| uuid::Uuid::parse_str(s).is_ok()),
            "invalid Codex thread"
        );
        let target = destination(c, source, role);
        let pattern = format!(
            r"^{}-{}(?:-cli(?:-v[1-9][0-9]*)?)?$",
            regex::escape(source),
            regex::escape(role)
        );
        ensure!(
            regex::Regex::new(&pattern)?.is_match(&target),
            "binding outside source/role scope"
        );
    }
    for (id, role) in threads {
        ensure!(
            numeric(id)
                && role
                    .as_str()
                    .is_some_and(|r| roles.get(r).is_some_and(|v| v.is_string())),
            "unregistered Discord thread"
        );
    }
    // These optional legacy observers are not enabled in either adopted deployment.
    // Refuse the configuration rather than silently dropping lifecycle work.
    ensure!(
        c["request_lifecycle"] != true,
        "request_lifecycle observer is not supported by this native gateway; retain the existing adapter"
    );
    ensure!(
        expand(text(c, "state_file")).is_absolute(),
        "absolute adapter state_file required"
    );
    Ok(())
}
fn destination(c: &Value, source: &str, role: &str) -> String {
    c["monitor_bindings"][role]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{source}-{role}"))
}
fn parse(c: &Value, m: &Value) -> Option<Value> {
    let channel = text(m, "channel_id");
    let author = &m["author"];
    if text(m, "guild_id") != text(c, "guild_id")
        || author["bot"] == true
        || !m["webhook_id"].is_null()
    {
        return None;
    }
    if !matches!(m["type"].as_u64().unwrap_or(0), 0 | 19) {
        return None;
    }
    if c["threads_only"] == true && channel == text(c, "channel_id") {
        return None;
    }
    let role = c["discord_threads"][channel].as_str();
    if channel != text(c, "channel_id") && role.is_none() {
        return None;
    }
    if !(role.is_some() && c["allow_all_thread_users"] == true
        || c["allowed_user_ids"]
            .as_array()?
            .iter()
            .any(|v| v.as_str() == Some(text(author, "id"))))
    {
        return None;
    }
    if !numeric(text(m, "id")) || !numeric(text(author, "id")) {
        return None;
    }
    let mut body = text(m, "content").trim().to_string();
    if body.is_empty() && m["attachments"].as_array().is_some_and(|a| !a.is_empty()) {
        body = "Please inspect the attached images.".into();
    }
    let re = regex::Regex::new(r"(?s)^!([a-z]+)\s+(.+)$").ok()?;
    let capture = re.captures(&body);
    let selected = if let Some(role) = role {
        if body.chars().count() > 6000 {
            return None;
        }
        if capture.as_ref().is_some_and(|v| &v[1] != role) {
            return None;
        }
        role.to_string()
    } else {
        capture.as_ref()?.get(1)?.as_str().to_string()
    };
    if let Some(capture) = capture {
        body = capture[2].trim().to_string();
    }
    if body.is_empty() || body.chars().count() > 6000 || !c["roles"][&selected].is_string() {
        return None;
    }
    Some(
        json!({"id":m["id"],"role":selected,"body":body,"author_id":author["id"],"channel_id":channel}),
    )
}
fn schema(db: &Connection) -> Result<()> {
    db.busy_timeout(Duration::from_secs(5))?;
    db.execute_batch("CREATE TABLE IF NOT EXISTS message_routes(id TEXT PRIMARY KEY,channel_id TEXT,role TEXT);
      CREATE TABLE IF NOT EXISTS gateway_pending(id TEXT PRIMARY KEY,target TEXT,payload TEXT,state TEXT,attempts INTEGER DEFAULT 0,error TEXT,next_at REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS gateway_native_raw(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS gateway_native_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS gateway_native_cursors(channel TEXT PRIMARY KEY,id TEXT NOT NULL);")?;
    let columns = db
        .prepare("PRAGMA table_info(gateway_pending)")?
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (n, t) in [
        ("receipt", "TEXT"),
        ("seen_reacted", "INTEGER NOT NULL DEFAULT 1"),
        ("seen_attempts", "INTEGER DEFAULT 0"),
        ("seen_next_at", "REAL DEFAULT 0"),
    ] {
        if !columns.iter().any(|c| c == n) {
            db.execute_batch(&format!("ALTER TABLE gateway_pending ADD COLUMN {n} {t}"))?;
        }
    }
    Ok(())
}
struct Gateway {
    config: Value,
    source: String,
    state: PathBuf,
    db: PathBuf,
    token: String,
    source_token: String,
    base: String,
    api: String,
    client: Client,
}
impl Gateway {
    fn open(&self) -> Result<Connection> {
        let db = Connection::open(&self.db)?;
        db.busy_timeout(Duration::from_secs(5))?;
        Ok(db)
    }
    fn health(&self, ready: bool, reason: &str) -> Result<()> {
        let queued: i64 =
            self.open()?
                .query_row("SELECT count(*) FROM gateway_native_raw", [], |r| r.get(0))?;
        write_private(
            &self.db.with_file_name("gateway-health.json"),
            &serde_json::to_vec(
                &json!({"runtime":"rust","pid":std::process::id(),"checked_at":now(),"gateway_ready":ready,"channel_id":self.config["channel_id"],"raw_pending":queued,"status":reason}),
            )?,
        )
    }
    async fn rest(&self, method: Method, path: &str) -> Result<Value> {
        for _ in 0..3 {
            let r = self
                .client
                .request(method.clone(), format!("{}{path}", self.api))
                .header("Authorization", format!("Bot {}", self.token))
                .send()
                .await
                .map_err(|_| anyhow::anyhow!("Discord transport unavailable"))?;
            let status = r.status();
            if status.as_u16() == 429 {
                let v: Value = r.json().await.context("rate limit response")?;
                tokio::time::sleep(Duration::from_secs_f64(
                    v["retry_after"].as_f64().unwrap_or(5.0).clamp(0.1, 60.0),
                ))
                .await;
                continue;
            }
            ensure!(status.is_success(), "Discord HTTP {}", status.as_u16());
            if status.as_u16() == 204 {
                return Ok(Value::Null);
            }
            return r.json().await.context("invalid Discord response");
        }
        bail!("Discord rate limit retry budget exhausted")
    }
    async fn verify(&self) -> Result<()> {
        let me = self.rest(Method::GET, "/users/@me").await?;
        ensure!(
            me["bot"] == true && me["id"] == self.config["application_id"],
            "bot identity mismatch"
        );
        let channel = self
            .rest(
                Method::GET,
                &format!("/channels/{}", text(&self.config, "channel_id")),
            )
            .await?;
        ensure!(
            channel["guild_id"] == self.config["guild_id"],
            "guild mismatch"
        );
        for id in self.config["discord_threads"]
            .as_object()
            .context("threads")?
            .keys()
        {
            let ch = self.rest(Method::GET, &format!("/channels/{id}")).await?;
            ensure!(
                ch["parent_id"] == self.config["channel_id"]
                    && ch["guild_id"] == self.config["guild_id"],
                "registered thread has wrong parent/guild"
            );
        }
        Ok(())
    }
    fn enqueue(&self, m: &Value) -> Result<()> {
        let Some(_) = parse(&self.config, m) else {
            return Ok(());
        };
        let db = self.open()?;
        db.execute("INSERT OR IGNORE INTO gateway_native_raw(id,payload) SELECT ?,? WHERE NOT EXISTS(SELECT 1 FROM gateway_pending WHERE id=?)",params![text(m,"id"),m.to_string(),text(m,"id")])?;
        Ok(())
    }
    async fn catch_up(&self, seed: &str) -> Result<()> {
        let mut channels: Vec<String> = self.config["discord_threads"]
            .as_object()
            .context("threads")?
            .keys()
            .cloned()
            .collect();
        if self.config["threads_only"] != true {
            channels.push(text(&self.config, "channel_id").into());
        }
        for channel in channels {
            let mut after: String = self
                .open()?
                .query_row(
                    "SELECT id FROM gateway_native_cursors WHERE channel=?",
                    [&channel],
                    |r| r.get(0),
                )
                .optional()?
                .unwrap_or_else(|| seed.into());
            let mut complete = false;
            for _ in 0..100 {
                let messages = self
                    .rest(
                        Method::GET,
                        &format!("/channels/{channel}/messages?limit=100&after={after}"),
                    )
                    .await?;
                let mut rows = messages.as_array().context("message history")?.clone();
                rows.sort_by_key(|m| text(m, "id").parse::<u64>().unwrap_or(0));
                for mut m in rows.iter().cloned() {
                    m["guild_id"] = self.config["guild_id"].clone();
                    self.enqueue(&m)?;
                    after = text(&m, "id").to_string();
                    ensure!(numeric(&after), "invalid history ID");
                }
                self.open()?.execute("INSERT INTO gateway_native_cursors VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET id=excluded.id",params![channel,after])?;
                if rows.len() < 100 {
                    complete = true;
                    break;
                }
            }
            ensure!(
                complete,
                "history recovery exceeds 100 pages; operator review required"
            );
        }
        Ok(())
    }
    async fn images(&self, m: &Value) -> Vec<Value> {
        let Some(attachments) = m["attachments"].as_array() else {
            return vec![];
        };
        let mut results = Vec::new();
        for a in attachments.iter().take(4) {
            let outcome:Result<Value>=async {
                ensure!(a["size"].as_u64().is_some_and(|n|n<=MAX_IMAGE as u64),"image size");
                let url=url::Url::parse(text(a,"url"))?;
                ensure!(url.scheme()=="https" && matches!(url.host_str(),Some("cdn.discordapp.com"|"media.discordapp.net")) && url.username().is_empty(),"attachment host");
                ensure!(numeric(text(a,"id")) && numeric(text(m,"id")),"attachment ID");
                let mut response=self.client.get(url).send().await?.error_for_status()?;
                let mut data=Vec::new();
                while let Some(chunk)=response.chunk().await? {ensure!(data.len()+chunk.len()<=MAX_IMAGE,"image too large");data.extend_from_slice(&chunk);}
                let ext=image_extension(&data)?;
                let dir=self.db.parent().context("state parent")?.join("incoming-images").join(text(m,"id"));
                use std::os::unix::fs::PermissionsExt;
                fs::create_dir_all(&dir)?;fs::set_permissions(&dir,fs::Permissions::from_mode(0o700))?;
                let path=dir.join(format!("{}{ext}",text(a,"id")));
                write_private(&path,&data)?;
                Ok(json!({"path":path,"sha256":format!("{:x}",Sha256::digest(&data)),"bytes":data.len()}))
            }.await;
            results.push(outcome.unwrap_or_else(
                |_| json!({"error":"Attachment unavailable or not a PNG/JPEG within 8 MiB"}),
            ));
        }
        if attachments.len() > 4 {
            results.push(json!({"error":"Only first 4 attachments processed"}));
        }
        results
    }
    async fn prepare(&self) -> Result<()> {
        let rows: Vec<String> = self
            .open()?
            .prepare("SELECT payload FROM gateway_native_raw ORDER BY length(id),id LIMIT 8")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        for raw in rows {
            let m: Value = serde_json::from_str(&raw)?;
            let mut command =
                parse(&self.config, &m).context("persisted message no longer authorized")?;
            let images = timeout(Duration::from_secs(20), self.images(&m))
                .await
                .unwrap_or_else(|_| vec![json!({"error":"Attachment download timed out"})]);
            if !images.is_empty() {
                command["body"] = json!(format!(
                    "{}\nAttached images (untrusted user content): {}\nOpen images before describing them; do not claim to see unavailable files.",
                    text(&command, "body"),
                    serde_json::to_string(&images)?
                ));
            }
            let role = text(&command, "role");
            let target = destination(&self.config, &self.source, role);
            let payload = json!({"id":m["id"],"source":self.source,"type":"discord.command","data":{"message":command["body"],"role":role,"author_id":command["author_id"],"channel_id":command["channel_id"],"reply_to":m["id"]}});
            let mut db = self.open()?;
            let tx = db.transaction()?;
            tx.execute(
                "INSERT OR IGNORE INTO message_routes VALUES(?,?,?)",
                params![text(&m, "id"), text(&command, "channel_id"), role],
            )?;
            tx.execute("INSERT OR IGNORE INTO gateway_pending(id,target,payload,state,seen_reacted) VALUES(?,?,?,'pending',0)",params![text(&m,"id"),target,payload.to_string()])?;
            tx.execute(
                "DELETE FROM gateway_native_raw WHERE id=?",
                [text(&m, "id")],
            )?;
            tx.commit()?;
        }
        Ok(())
    }
    async fn deliver(&self) -> Result<()> {
        let rows:Vec<(String,String,String,u32)>=self.open()?.prepare("SELECT id,target,payload,attempts FROM gateway_pending WHERE state='pending' AND (?=0 OR attempts<?) AND next_at<=? ORDER BY length(id),id LIMIT 8")?.query_map(params![self.config["delivery_max_attempts"].as_u64().unwrap_or(5),self.config["delivery_max_attempts"].as_u64().unwrap_or(5),now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
        for (id, target, payload, attempts) in rows {
            let result: Result<String> = async {
                let payload: Value = serde_json::from_str(&payload)?;
                ensure!(
                    target
                        == destination(&self.config, &self.source, text(&payload["data"], "role")),
                    "stale route requires review"
                );
                let r = self
                    .client
                    .post(format!("{}/v1/events/{target}", self.base))
                    .bearer_auth(&self.source_token)
                    .json(&payload)
                    .send()
                    .await?;
                ensure!(r.status().as_u16() == 202, "receiver rejected event");
                let v: Value = r.json().await?;
                Ok(v["delivery_id"].as_str().context("missing receipt")?.into())
            }
            .await;
            match result {
                Ok(receipt) => {
                    self.open()?.execute("UPDATE gateway_pending SET state='delivered',receipt=?,error=NULL WHERE id=?",params![receipt,id])?;
                }
                Err(_) => {
                    self.open()?.execute("UPDATE gateway_pending SET attempts=attempts+1,error='delivery_unavailable',next_at=? WHERE id=?",params![now()+2f64.powi((attempts+1).min(6) as i32),id])?;
                }
            }
        }
        Ok(())
    }
    async fn seen(&self) -> Result<()> {
        let rows:Vec<(String,String,String,u32)>=self.open()?.prepare("SELECT p.id,p.receipt,r.channel_id,p.seen_attempts FROM gateway_pending p JOIN message_routes r ON r.id=p.id WHERE p.state='delivered' AND p.receipt IS NOT NULL AND p.seen_reacted=0 AND p.seen_attempts<5 AND p.seen_next_at<=? LIMIT 8")?.query_map([now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
        for (id, receipt, channel, attempts) in rows {
            let result: Result<bool> = async {
                let output = timeout(
                    Duration::from_secs(10),
                    tokio::process::Command::new(std::env::current_exe()?)
                        .args([
                            "--state",
                            &self.state.to_string_lossy(),
                            "inspect",
                            &receipt,
                        ])
                        .kill_on_drop(true)
                        .output(),
                )
                .await??;
                ensure!(output.status.success(), "inspection failed");
                let v: Value = serde_json::from_slice(&output.stdout)?;
                if v["native"]["state"] != "consumed" {
                    return Ok(false);
                }
                ensure!(
                    self.config["discord_threads"][&channel].is_string()
                        || channel == text(&self.config, "channel_id"),
                    "unregistered reaction target"
                );
                if let Some(emoji) = self.config["receipt_emoji"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                {
                    let encoded: String =
                        url::form_urlencoded::byte_serialize(emoji.as_bytes()).collect();
                    self.rest(
                        Method::PUT,
                        &format!("/channels/{channel}/messages/{id}/reactions/{encoded}/@me"),
                    )
                    .await?;
                }
                Ok(true)
            }
            .await;
            match result {
                Ok(true) => {
                    self.open()?
                        .execute("UPDATE gateway_pending SET seen_reacted=1 WHERE id=?", [id])?;
                }
                Ok(false) => {}
                Err(_) => {
                    self.open()?.execute("UPDATE gateway_pending SET seen_attempts=seen_attempts+1,seen_next_at=? WHERE id=?",params![now()+2f64.powi((attempts+1).min(8) as i32),id])?;
                }
            }
        }
        Ok(())
    }
}
fn image_extension(data: &[u8]) -> Result<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Ok(".png")
    } else if data.starts_with(b"\xff\xd8\xff") {
        Ok(".jpg")
    } else {
        bail!("unsupported image")
    }
}
fn cleanup_images(root: &Path) {
    let Ok(dirs) = fs::read_dir(root) else { return };
    for dir in dirs.flatten().take(10000) {
        if !dir.file_type().is_ok_and(|t| t.is_dir()) {
            continue;
        }
        let Ok(files) = fs::read_dir(dir.path()) else {
            continue;
        };
        for f in files.flatten().take(10) {
            if f.file_type().is_ok_and(|t| t.is_file())
                && f.metadata()
                    .ok()
                    .and_then(|m| m.modified().ok())
                    .and_then(|t| t.elapsed().ok())
                    .is_some_and(|d| d > Duration::from_secs(86400))
            {
                let _ = fs::remove_file(f.path());
            }
        }
    }
}
#[derive(Default, serde::Serialize, serde::Deserialize)]
struct Session {
    id: Option<String>,
    url: Option<String>,
    seq: Option<u64>,
}
impl Session {
    fn auth(&self, token: &str) -> Value {
        if let (Some(id), Some(seq)) = (&self.id, self.seq) {
            json!({"op":6,"d":{"token":token,"session_id":id,"seq":seq}})
        } else {
            json!({"op":2,"d":{"token":token,"intents":33281,"properties":{"os":std::env::consts::OS,"browser":"codex-monitor","device":"codex-monitor"}}})
        }
    }
}
async fn connection(
    g: &Arc<Gateway>,
    session: &mut Session,
    gateway: &str,
    seed: &str,
) -> Result<()> {
    let mut url = url::Url::parse(session.url.as_deref().unwrap_or(gateway))?;
    ensure!(
        url.scheme() == "wss"
            && url
                .host_str()
                .is_some_and(|h| h == "gateway.discord.gg" || h.ends_with(".discord.gg"))
            || cfg!(test) && url.scheme() == "ws" && url.host_str() == Some("127.0.0.1"),
        "invalid gateway URL"
    );
    url.set_query(Some("v=10&encoding=json"));
    let (mut ws, _) = timeout(
        Duration::from_secs(20),
        tokio_tungstenite::connect_async(url.as_str()),
    )
    .await??;
    let hello = timeout(Duration::from_secs(20), ws.next())
        .await?
        .context("missing hello")??;
    let hello: Value = serde_json::from_str(hello.to_text()?)?;
    ensure!(hello["op"] == 10, "missing gateway hello");
    let interval = Duration::from_millis(
        hello["d"]["heartbeat_interval"]
            .as_u64()
            .filter(|n| *n > 0)
            .context("heartbeat interval")?,
    );
    let mut heart = tokio::time::interval_at(
        Instant::now() + interval.mul_f64(rand::random::<f64>()),
        interval,
    );
    let mut health = tokio::time::interval(Duration::from_secs(15));
    let mut ack = true;
    let mut ready = false;
    let mut recovering = tokio::task::JoinSet::new();
    ws.send(Message::Text(session.auth(&g.token).to_string().into()))
        .await?;
    loop {
        tokio::select! {
            _=heart.tick()=>{ensure!(ack,"heartbeat ACK missing");ack=false;ws.send(Message::Text(json!({"op":1,"d":session.seq}).to_string().into())).await?;}
            _=health.tick()=>{g.health(ready,if ready {"ready"} else {"connecting"})?;}
            recovered=recovering.join_next(), if !recovering.is_empty()=>{recovered.context("history task missing")???;ready=true;g.health(true,"ready")?;}
            raw=ws.next()=>{
                let raw=raw.context("gateway disconnected")??;
                if let Message::Close(close)=raw {
                    if let Some(close)=close {let code=u16::from(close.code); if matches!(code,4007|4009) {*session=Session::default();} else if matches!(code,4004|4010..=4014) {g.health(false,"fatal_gateway_configuration")?;bail!("fatal gateway code {code}");}}
                    bail!("gateway closed");
                }
                if let Message::Ping(p)=raw {ws.send(Message::Pong(p)).await?;continue;}
                if !raw.is_text() {continue;}
                let v:Value=serde_json::from_str(raw.to_text()?)?;
                match v["op"].as_u64() {
                    Some(11)=>ack=true,
                    Some(1)=>{ws.send(Message::Text(json!({"op":1,"d":session.seq}).to_string().into())).await?;},
                    Some(7)=>bail!("gateway requested reconnect"),
                    Some(9)=>{if v["d"]!=true {*session=Session::default();}bail!("gateway session invalidated");},
                    Some(0)=>{
                        match text(&v,"t") {
                            "READY"=>{ensure!(v["d"]["user"]["id"]==g.config["application_id"],"gateway identity mismatch");session.id=Some(text(&v["d"],"session_id").into());session.url=Some(text(&v["d"],"resume_gateway_url").into());let cloned=g.clone();let seed=seed.to_owned();recovering.spawn(async move {cloned.catch_up(&seed).await});},
                            "RESUMED"=>{let cloned=g.clone();let seed=seed.to_owned();recovering.spawn(async move {cloned.catch_up(&seed).await});},
                            "MESSAGE_CREATE"=>g.enqueue(&v["d"] )?,
                            _=>{}
                        }
                        if let Some(seq)=v["s"].as_u64() {session.seq=Some(seq);}
                        // Persist sequence only after the raw dispatch is durable.
                        let db=g.open()?;db.execute("INSERT INTO gateway_native_meta VALUES('session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[serde_json::to_string(session)?])?;
                    }
                    _=>{}
                }
                if ready {g.health(true,"ready")?;}
            }
        }
    }
}
pub async fn run(
    config: &Path,
    source: &str,
    state: &Path,
    check: bool,
    verify: bool,
    since: Option<&str>,
) -> Result<()> {
    let c: Value = serde_json::from_slice(&fs::read(config)?)?;
    validate(&c, source)?;
    let db = expand(text(&c, "state_file"));
    let token = secret(&expand(text(&c, "token_file")))?;
    let source_token = secret(&state.join(format!("source-{source}.token")))?;
    let receiver: Value = serde_json::from_slice(&fs::read(state.join("config.json"))?)?;
    let port = receiver["port"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= 65535)
        .context("receiver port")?;
    let g = Arc::new(Gateway {
        config: c,
        source: source.into(),
        state: state.into(),
        db,
        token,
        source_token,
        base: format!("http://127.0.0.1:{port}"),
        api: API.into(),
        client: Client::builder()
            .timeout(Duration::from_secs(20))
            .redirect(reqwest::redirect::Policy::none())
            .user_agent("codex-monitor/0.2.0")
            .build()?,
    });
    if verify {
        g.verify().await?;
        println!(
            "{}",
            json!({"verified":true,"runtime":"rust","source":source})
        );
        return Ok(());
    }
    if check {
        println!(
            "{}",
            json!({"valid":true,"runtime":"rust","source":source,"request_lifecycle":false})
        );
        return Ok(());
    }
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(g.db.with_extension("gateway.lock"))?;
    lock.try_lock_exclusive()
        .context("gateway already running for this adapter")?;
    schema(&g.open()?)?;
    let saved: Option<String> = g
        .open()?
        .query_row(
            "SELECT value FROM gateway_native_meta WHERE key='seed'",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let seed = saved
        .or_else(|| since.map(str::to_owned))
        .context("first startup requires --since-id for bounded cutover recovery")?;
    ensure!(numeric(&seed), "invalid cutover message ID");
    g.open()?.execute(
        "INSERT OR IGNORE INTO gateway_native_meta VALUES('seed',?)",
        [&seed],
    )?;
    let saved: Option<String> = g
        .open()?
        .query_row(
            "SELECT value FROM gateway_native_meta WHERE key='session'",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let mut session: Session = saved
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    let workers = g.clone();
    let task = tokio::spawn(async move {
        let mut ticks = 0u32;
        loop {
            let result: Result<()> = async {
                workers.prepare().await?;
                workers.deliver().await?;
                workers.seen().await?;
                Ok(())
            }
            .await;
            if result.is_err() {
                eprintln!("gateway worker cycle failed; durable records retained");
            }
            ticks += 1;
            if ticks.is_multiple_of(1800) {
                cleanup_images(&workers.db.with_file_name("incoming-images"));
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    });
    let mut stop = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let operation = async {
        let mut failures = 0u32;
        loop {
            g.health(false, "connecting")?;
            let result: Result<()> = async {
                g.verify().await?;

                let info = g.rest(Method::GET, "/gateway/bot").await?;
                ensure!(
                    info["session_start_limit"]["remaining"]
                        .as_u64()
                        .unwrap_or(1)
                        > 0
                        || session.id.is_some(),
                    "gateway session limit exhausted"
                );
                connection(&g, &mut session, text(&info, "url"), &seed).await
            }
            .await;
            if let Err(e) = result {
                g.health(false, "reconnecting")?;
                // Errors are intentionally categorical: never serialize transport URLs or tokens.
                eprintln!("Discord gateway reconnect pending");
                if e.to_string().starts_with("fatal gateway code") {
                    return Err(e);
                }
            }
            g.open()?.execute("INSERT INTO gateway_native_meta VALUES('session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[serde_json::to_string(&session)?])?;
            failures = failures.saturating_add(1);
            tokio::time::sleep(Duration::from_secs(2u64.pow(failures.min(6)).max(5))).await;
        }
    };
    let result =
        tokio::select! {r=operation=>r,_=stop.recv()=>Ok(()),_=tokio::signal::ctrl_c()=>Ok(())};
    task.abort();
    let _ = task.await;
    g.health(false, "stopped")?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Value {
        json!({"project_name":"Example","guild_id":"11111111111111111","channel_id":"22222222222222222","application_id":"33333333333333333","allowed_user_ids":["44444444444444444"],"roles":{"pm":"00000000-0000-4000-8000-000000000001"},"discord_threads":{"55555555555555555":"pm"},"threads_only":true,"state_file":"/tmp/example.sqlite3"})
    }
    fn message() -> Value {
        json!({"id":"66666666666666666","guild_id":"11111111111111111","channel_id":"55555555555555555","author":{"id":"44444444444444444","bot":false},"content":"hello","type":0})
    }
    #[test]
    fn routes_and_authorization() {
        let c = config();
        validate(&c, "discord-example").unwrap();
        assert!(parse(&c, &message()).is_some());
        for (key, value) in [
            ("channel_id", json!("22222222222222222")),
            ("guild_id", json!("99999999999999999")),
            ("webhook_id", json!("77777777777777777")),
            ("content", json!("!worker do work")),
        ] {
            let mut m = message();
            m[key] = value;
            assert!(parse(&c, &m).is_none());
        }
        let mut m = message();
        m["author"]["bot"] = json!(true);
        assert!(parse(&c, &m).is_none());
        m["author"]["bot"] = json!(false);
        m["author"]["id"] = json!("99999999999999999");
        assert!(parse(&c, &m).is_none());
        let mut c = c;
        c["allow_all_thread_users"] = json!(true);
        assert!(parse(&c, &m).is_some());
    }
    #[test]
    fn configuration_rejects_unsupported_lifecycle_and_foreign_routes() {
        let mut c = config();
        c["request_lifecycle"] = json!(true);
        assert!(validate(&c, "discord-example").is_err());
        c["request_lifecycle"] = json!(false);
        c["monitor_bindings"] = json!({"pm":"other-project"});
        assert!(validate(&c, "discord-example").is_err());
    }
    #[test]
    fn upgrade_preserves_legacy_receipts() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE gateway_pending(id TEXT PRIMARY KEY,target TEXT,payload TEXT,state TEXT,attempts INTEGER DEFAULT 0,error TEXT,next_at REAL DEFAULT 0);INSERT INTO gateway_pending(id,state) VALUES('existing','delivered');").unwrap();
        schema(&db).unwrap();
        schema(&db).unwrap();
        assert_eq!(
            db.query_row(
                "SELECT state FROM gateway_pending WHERE id='existing'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "delivered"
        );
        assert_eq!(
            db.query_row("SELECT seen_reacted FROM gateway_pending", [], |r| r
                .get::<_, u32>(0))
                .unwrap(),
            1
        );
    }
    #[test]
    fn resume_and_identify() {
        let mut s = Session::default();
        assert_eq!(s.auth("secret")["op"], 2);
        s.id = Some("session".into());
        s.seq = Some(42);
        assert_eq!(s.auth("secret")["op"], 6);
        assert_eq!(s.auth("secret")["d"]["seq"], 42);
    }
    #[test]
    fn bounded_image_types() {
        assert_eq!(image_extension(b"\x89PNG\r\n\x1a\nrest").unwrap(), ".png");
        assert_eq!(image_extension(b"\xff\xd8\xffrest").unwrap(), ".jpg");
        assert!(image_extension(b"<script>").is_err());
    }

    fn fixture(root: &Path, api: String) -> Gateway {
        Gateway {
            config: config(),
            source: "discord-example".into(),
            state: root.into(),
            db: root.join("bridge.sqlite3"),
            token: "fixture-token".into(),
            source_token: "fixture-source".into(),
            base: api.clone(),
            api,
            client: Client::builder()
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap(),
        }
    }
    async fn history_server() -> (String, tokio::task::JoinHandle<()>) {
        let app = axum::Router::new().route(
            "/channels/{id}/messages",
            axum::routing::get(|| async { axum::Json(json!([])) }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (url, task)
    }
    #[tokio::test]
    async fn durable_input_is_not_replayed_after_restart() {
        let dir = tempfile::tempdir().unwrap();
        let g = fixture(dir.path(), "http://127.0.0.1:1".into());
        schema(&g.open().unwrap()).unwrap();
        g.enqueue(&message()).unwrap();
        g.enqueue(&message()).unwrap();
        // Reopening the adapter simulates a crash after the raw dispatch commit.
        let g = fixture(dir.path(), "http://127.0.0.1:1".into());
        g.prepare().await.unwrap();
        g.enqueue(&message()).unwrap();
        g.prepare().await.unwrap();
        let db = g.open().unwrap();
        assert_eq!(
            db.query_row("SELECT count(*) FROM gateway_pending", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM gateway_native_raw", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            db.query_row("SELECT role FROM message_routes", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            "pm"
        );
    }
    #[tokio::test]
    async fn failed_delivery_retains_identity_and_event_for_retry() {
        let dir = tempfile::tempdir().unwrap();
        let ids = Arc::new(std::sync::Mutex::new(Vec::new()));
        let recorded = ids.clone();
        let app = axum::Router::new().route(
            "/v1/events/discord-example-pm",
            axum::routing::post(
                move |headers: axum::http::HeaderMap, axum::Json(body): axum::Json<Value>| {
                    let recorded = recorded.clone();
                    async move {
                        assert_eq!(headers["authorization"], "Bearer fixture-source");
                        let mut ids = recorded.lock().unwrap();
                        ids.push(body["id"].clone());
                        let status = if ids.len() == 1 {
                            axum::http::StatusCode::SERVICE_UNAVAILABLE
                        } else {
                            axum::http::StatusCode::ACCEPTED
                        };
                        (status, axum::Json(json!({"delivery_id":"stable-receipt"})))
                    }
                },
            ),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let g = fixture(dir.path(), base);
        schema(&g.open().unwrap()).unwrap();
        g.enqueue(&message()).unwrap();
        g.prepare().await.unwrap();
        g.deliver().await.unwrap();
        assert_eq!(
            g.open()
                .unwrap()
                .query_row("SELECT state FROM gateway_pending", [], |r| r
                    .get::<_, String>(0))
                .unwrap(),
            "pending"
        );
        g.open()
            .unwrap()
            .execute("UPDATE gateway_pending SET next_at=0", [])
            .unwrap();
        g.deliver().await.unwrap();
        g.deliver().await.unwrap();
        assert_eq!(
            g.open()
                .unwrap()
                .query_row("SELECT receipt FROM gateway_pending", [], |r| r
                    .get::<_, String>(0))
                .unwrap(),
            "stable-receipt"
        );
        let ids = ids.lock().unwrap();
        assert_eq!(ids.len(), 2);
        assert_eq!(ids[0], ids[1]);
        server.abort();
    }
    #[tokio::test]
    async fn gateway_protocol_persists_sequence_after_dispatch_and_resumes() {
        let dir = tempfile::tempdir().unwrap();
        let (api, history) = history_server().await;
        let g = Arc::new(fixture(dir.path(), api));
        schema(&g.open().unwrap()).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}", listener.local_addr().unwrap());
        let resume = url.clone();
        let server = tokio::spawn(async move {
            for cycle in 0..2 {
                let (stream, _) = listener.accept().await.unwrap();
                let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                ws.send(Message::Text(
                    json!({"op":10,"d":{"heartbeat_interval":1000}})
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                let auth: Value =
                    serde_json::from_str(ws.next().await.unwrap().unwrap().to_text().unwrap())
                        .unwrap();
                assert_eq!(auth["op"], if cycle == 0 { 2 } else { 6 });
                if cycle == 1 {
                    assert_eq!(auth["d"]["seq"], 42);
                }
                ws.send(Message::Text(json!({"op":0,"s":41,"t":"READY","d":{"user":{"id":"33333333333333333"},"session_id":"fixture-session","resume_gateway_url":resume}}).to_string().into())).await.unwrap();
                ws.send(Message::Text(
                    json!({"op":0,"s":42,"t":"MESSAGE_CREATE","d":message()})
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                ws.send(Message::Text(json!({"op":7}).to_string().into()))
                    .await
                    .unwrap();
            }
        });
        let mut session = Session::default();
        for _ in 0..2 {
            assert!(
                timeout(
                    Duration::from_secs(5),
                    connection(&g, &mut session, &url, "60000000000000000")
                )
                .await
                .unwrap()
                .is_err()
            );
        }
        server.await.unwrap();
        history.abort();
        assert_eq!(session.seq, Some(42));
        assert_eq!(
            g.open()
                .unwrap()
                .query_row("SELECT count(*) FROM gateway_native_raw", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        let saved: String = g
            .open()
            .unwrap()
            .query_row(
                "SELECT value FROM gateway_native_meta WHERE key='session'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Session>(&saved).unwrap().seq,
            Some(42)
        );
    }
}
