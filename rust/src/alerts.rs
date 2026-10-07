//! State-change notices only; no approvals, input replay or account refresh.
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    time::Duration,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    Ready,
    Auth,
    Disconnected,
    Unloaded,
    Execution,
    Approval,
    Input,
    Both,
    Unknown,
}
impl State {
    fn label(self) -> &'static str {
        match self {
            Self::Ready => "Available again (not proof of completed work)",
            Self::Auth => "Login required",
            Self::Disconnected => "Connection lost",
            Self::Unloaded => "Conversation unloaded",
            Self::Execution => "Execution error",
            Self::Approval => "Approval needed",
            Self::Input => "Response needed",
            Self::Both => "Approval and response needed",
            Self::Unknown => "Status could not be verified",
        }
    }
    fn problem(self) -> bool {
        !matches!(self, Self::Ready | Self::Unknown)
    }
    fn debounce(self) -> bool {
        matches!(
            self,
            Self::Disconnected | Self::Unloaded | Self::Unknown | Self::Ready
        )
    }
}
fn observed(health: &Value) -> State {
    match health["status"]
        .as_str()
        .or_else(|| health["state"].as_str())
    {
        Some("auth-required") => State::Auth,
        Some("unavailable") => State::Disconnected,
        Some("unloaded") => State::Unloaded,
        Some("execution-error") if health["execution_error"]["kind"] == "authentication" => {
            State::Auth
        }
        Some("execution-error") => State::Execution,
        Some("waiting-for-approval") if health["attention"]["user_input_required"] == true => {
            State::Both
        }
        Some("waiting-for-approval") => State::Approval,
        Some("waiting-for-user-input") => State::Input,
        Some("ready-to-receive") => State::Ready,
        _ => State::Unknown,
    }
}
#[derive(Clone)]
struct Entry {
    confirmed: Option<State>,
    candidate: State,
    count: u8,
}
#[derive(Default)]
pub struct Tracker {
    entries: BTreeMap<String, Entry>,
}
impl Tracker {
    pub fn update(&mut self, rows: &[Value], receiver: &Value) -> Vec<String> {
        let mut current = BTreeMap::new();
        for row in rows {
            for route in row["routes"].as_array().into_iter().flatten() {
                if route["enabled"] != true || route["removed"] == true {
                    continue;
                }
                let (Some(endpoint), Some(thread)) =
                    (route["endpoint"].as_str(), route["thread"].as_str())
                else {
                    continue;
                };
                // JSON encoding keeps endpoint/thread pairs unambiguous. Shared routes
                // collapse to one notice; no endpoint or token is put in the popup.
                let key = serde_json::to_string(&(endpoint, thread)).unwrap();
                let name = format!(
                    "{} / {}",
                    row["project"].as_str().unwrap_or("Ungrouped"),
                    row["name"].as_str().unwrap_or("Conversation")
                );
                current
                    .entry(key)
                    .or_insert((name, observed(&route["owner_health"])));
            }
        }
        current.insert(
            "receiver".into(),
            (
                "Event receiver".into(),
                if receiver["ready"] == true {
                    State::Ready
                } else {
                    State::Disconnected
                },
            ),
        );
        let present: BTreeSet<_> = current.keys().cloned().collect();
        self.entries.retain(|key, _| present.contains(key));
        let mut notices = Vec::new();
        for (key, (name, state)) in current {
            let entry = self.entries.entry(key).or_insert(Entry {
                confirmed: None,
                candidate: state,
                count: 0,
            });
            if entry.candidate == state {
                entry.count = entry.count.saturating_add(1);
            } else {
                entry.candidate = state;
                entry.count = 1;
            }
            if entry.confirmed == Some(state) || state.debounce() && entry.count < 2 {
                continue;
            }
            let previous = entry.confirmed;
            // An unobservable state must not clear an actionable warning or become
            // a false recovery; repeated unknowns can still report lost visibility.
            if state == State::Unknown && previous.is_none() {
                continue;
            }
            entry.confirmed = Some(state);
            if state.problem()
                || state == State::Unknown
                || state == State::Ready
                    && previous.is_some_and(|previous| previous != State::Ready)
            {
                notices.push(format!("{}: {}", safe_text(&name, 100), state.label()));
            }
        }
        notices
    }
}
fn safe_text(text: &str, limit: usize) -> String {
    text.chars()
        .filter(|c| {
            !c.is_control() && !matches!(*c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .take(limit)
        .collect()
}
fn message(notices: &[String]) -> String {
    let mut text = notices
        .iter()
        .take(4)
        .map(|s| safe_text(s, 180))
        .collect::<Vec<_>>()
        .join(" | ");
    if notices.len() > 4 {
        text.push_str(&format!(" | +{} more; open monitor", notices.len() - 4));
    }
    text
}
fn terminal_sequence(body: &str, tmux: bool) -> String {
    let osc = format!("\x1b]9;Codex Monitor: {}\x07", safe_text(body, 850));
    if tmux {
        format!("\x1bPtmux;{}\x1b\\", osc.replace('\x1b', "\x1b\x1b"))
    } else {
        osc
    }
}

fn automatic_method(program: Option<&str>, windows_terminal: bool) -> &'static str {
    if windows_terminal || matches!(program, Some("iTerm.app" | "WezTerm" | "ghostty")) {
        "osc9"
    } else {
        "bel"
    }
}

pub struct Notifier {
    mode: String,
    sender: tokio::sync::mpsc::Sender<String>,
    worker: tokio::task::JoinHandle<()>,
    failures: std::sync::Arc<std::sync::atomic::AtomicU64>,
}
impl Notifier {
    pub fn new(mode: &str) -> Self {
        let mode = if mode == "auto" {
            automatic_method(
                std::env::var("TERM_PROGRAM").ok().as_deref(),
                std::env::var_os("WT_SESSION").is_some(),
            )
        } else {
            mode
        }
        .to_owned();
        let (sender, mut receiver) = tokio::sync::mpsc::channel::<String>(8);
        let failures = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
        let errors = failures.clone();
        let worker = tokio::spawn(async move {
            while let Some(body) = receiver.recv().await {
                if !desktop(&body).await {
                    errors.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
            }
        });
        Self {
            mode,
            sender,
            worker,
            failures,
        }
    }
    pub fn send(&self, notices: &[String]) -> std::io::Result<()> {
        if self.mode == "off" || notices.is_empty() {
            return Ok(());
        }
        let body = message(notices);
        // Bell remains a terminal/tab activity fallback even if desktop delivery
        // is unavailable. This writes only to the live dashboard's own terminal.
        let mut out = std::io::stdout().lock();
        if self.mode == "osc9" {
            out.write_all(terminal_sequence(&body, std::env::var_os("TMUX").is_some()).as_bytes())?;
        } else {
            out.write_all(b"\x07")?;
            if self.mode == "desktop" && self.sender.try_send(body).is_err() {
                self.failures
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        }
        out.flush()
    }
    pub fn failed(&self) -> bool {
        self.failures.swap(0, std::sync::atomic::Ordering::Relaxed) > 0
    }
}
impl Drop for Notifier {
    fn drop(&mut self) {
        self.worker.abort();
    }
}
async fn desktop(body: &str) -> bool {
    let mut command = if cfg!(target_os = "macos") {
        let mut command = tokio::process::Command::new("/usr/bin/osascript");
        command.args(["-e", "on run argv\ndisplay notification (item 1 of argv) with title \"Codex Monitor\"\nend run", body]);
        command
    } else if cfg!(target_os = "linux") {
        let mut command = tokio::process::Command::new("notify-send");
        let escaped = body
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;");
        command.args([
            "--app-name=codex-monitor",
            "--urgency=normal",
            "--expire-time=10000",
            "--",
            "Codex Monitor",
            &escaped,
        ]);
        command
    } else {
        return false;
    };
    command
        .kill_on_drop(true)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    matches!(tokio::time::timeout(Duration::from_secs(3), command.status()).await, Ok(Ok(status)) if status.success())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn rows(status: &str) -> Vec<Value> {
        vec![
            json!({"project":"Project", "name":"Work", "routes":[{"thread":"thread", "endpoint":"ws://127.0.0.1:1", "enabled":true, "owner_health":{"status":status}}]}),
        ]
    }
    fn receiver() -> Value {
        json!({"ready":true})
    }
    #[test]
    fn alerts_once_per_change_and_recovers_without_claiming_model_completion() {
        let mut tracker = Tracker::default();
        assert!(
            tracker
                .update(&rows("ready-to-receive"), &receiver())
                .is_empty()
        );
        assert!(
            tracker
                .update(&rows("ready-to-receive"), &receiver())
                .is_empty()
        );
        for status in [
            "auth-required",
            "waiting-for-approval",
            "waiting-for-user-input",
            "execution-error",
        ] {
            assert_eq!(tracker.update(&rows(status), &receiver()).len(), 1);
            assert!(tracker.update(&rows(status), &receiver()).is_empty());
        }
        assert!(
            tracker
                .update(&rows("ready-to-receive"), &receiver())
                .is_empty()
        );
        let recovery = tracker.update(&rows("ready-to-receive"), &receiver());
        assert_eq!(recovery.len(), 1);
        assert!(recovery[0].contains("not proof of completed work"));
        assert!(
            tracker
                .update(&rows("ready-to-receive"), &receiver())
                .is_empty()
        );
    }
    #[test]
    fn debounces_disconnects_and_unknown_does_not_look_recovered() {
        let mut tracker = Tracker::default();
        for _ in 0..2 {
            tracker.update(&rows("ready-to-receive"), &receiver());
        }
        assert!(tracker.update(&rows("unavailable"), &receiver()).is_empty());
        assert!(
            tracker
                .update(&rows("ready-to-receive"), &receiver())
                .is_empty()
        );
        assert!(tracker.update(&rows("unavailable"), &receiver()).is_empty());
        assert_eq!(tracker.update(&rows("unavailable"), &receiver()).len(), 1);
        assert!(tracker.update(&rows("unverified"), &receiver()).is_empty());
        let unverified = tracker.update(&rows("unverified"), &receiver());
        assert_eq!(unverified.len(), 1);
        assert!(!unverified[0].contains("Available again"));
        assert!(tracker.update(&rows("unverified"), &receiver()).is_empty());
        tracker.update(&rows("ready-to-receive"), &receiver());
        assert_eq!(
            tracker.update(&rows("ready-to-receive"), &receiver()).len(),
            1
        );
        assert!(
            Tracker::default()
                .update(&rows("unverified"), &receiver())
                .is_empty()
        );
    }
    #[test]
    fn deduplicates_routes_and_ignores_paused_removed_conversations() {
        let mut data = rows("auth-required");
        let route = data[0]["routes"][0].clone();
        data[0]["routes"].as_array_mut().unwrap().push(route);
        let mut tracker = Tracker::default();
        assert_eq!(tracker.update(&data, &receiver()).len(), 1);
        for route in data[0]["routes"].as_array_mut().unwrap() {
            route["enabled"] = json!(false);
        }
        assert!(tracker.update(&data, &receiver()).is_empty());
        assert!(tracker.entries.keys().all(|key| key == "receiver"));
        data[0]["routes"][0]["enabled"] = json!(true);
        data[0]["routes"][0]["removed"] = json!(true);
        assert!(tracker.update(&data, &receiver()).is_empty());
    }
    #[test]
    fn receiver_loss_and_recovery_are_independent_of_conversations() {
        let mut tracker = Tracker::default();
        let down = json!({"ready":false});
        assert!(tracker.update(&[], &down).is_empty());
        let notice = tracker.update(&[], &down);
        assert_eq!(notice.len(), 1);
        assert!(notice[0].contains("Event receiver: Connection lost"));
        assert!(tracker.update(&[], &down).is_empty());
        tracker.update(&[], &receiver());
        assert_eq!(tracker.update(&[], &receiver()).len(), 1);
    }
    #[test]
    fn auth_errors_and_combined_decisions_are_distinct() {
        let mut tracker = Tracker::default();
        let mut data = rows("execution-error");
        data[0]["routes"][0]["owner_health"]["execution_error"] =
            json!({"kind":"authentication","message":"token secret"});
        let alerts = tracker.update(&data, &receiver());
        assert!(alerts[0].contains("Login required"));
        assert!(!alerts[0].contains("secret"));
        data[0]["routes"][0]["owner_health"] =
            json!({"status":"waiting-for-approval", "attention":{"user_input_required":true}});
        assert!(tracker.update(&data, &receiver())[0].contains("Approval and response needed"));
    }
    #[test]
    fn auto_uses_terminal_notifications_with_a_portable_bell_fallback() {
        for program in ["iTerm.app", "WezTerm", "ghostty"] {
            assert_eq!(automatic_method(Some(program), false), "osc9");
        }
        assert_eq!(automatic_method(None, true), "osc9");
        assert_eq!(automatic_method(None, false), "bel");
        assert_eq!(automatic_method(Some("unknown"), false), "bel");
    }

    #[test]
    fn terminal_payload_is_bounded_and_cannot_inject_control_sequences() {
        let body = message(&vec!["bad\x1b]52;clipboard\x07\nname".into(); 6]);
        assert!(!body.contains('\x1b'));
        assert!(!body.contains('\x07'));
        assert!(!body.contains('\n'));
        assert!(body.contains("+2 more"));
        let plain = terminal_sequence(&body, false);
        assert!(plain.starts_with("\x1b]9;Codex Monitor:"));
        assert!(plain.ends_with("\x07"));
        let mux = terminal_sequence("test", true);
        assert!(mux.starts_with("\x1bPtmux;\x1b\x1b]9;"));
        assert!(terminal_sequence(&"x".repeat(2000), false).len() < 900);
    }
}
