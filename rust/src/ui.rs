use crate::{Config, connect, output, runtime, text};
use anyhow::{Context, Result};
use codex_monitor_rs::{session::SessionPool, store::Store};
use crossterm::{
    cursor,
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{self, ClearType},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{self, IsTerminal, Write},
    path::PathBuf,
    time::{Duration, Instant},
};

fn clean(s: &str, max: usize) -> String {
    let mut width = 0;
    s.chars()
        .filter(|c| !c.is_control())
        .take_while(|c| {
            width += unicode_width::UnicodeWidthChar::width(*c).unwrap_or(0);
            width <= max
        })
        .collect()
}
fn padded(s: &str, width: usize) -> String {
    let s = clean(s, width);
    format!(
        "{}{}",
        s,
        " ".repeat(width.saturating_sub(unicode_width::UnicodeWidthStr::width(s.as_str())))
    )
}
fn endpoint_can_open(endpoint: &str) -> bool {
    endpoint.starts_with("ws://")
        || endpoint.starts_with("wss://")
        || (endpoint.starts_with("unix:///") && endpoint.len() > "unix:///".len())
}
fn enter_hint(endpoint: &str) -> &'static str {
    if endpoint_can_open(endpoint) {
        "Enter open"
    } else if endpoint == "shared-local" {
        "Cannot attach; owner address needed"
    } else {
        "Cannot open; owner endpoint needed"
    }
}
fn enter_notice(endpoint: &str) -> Option<&'static str> {
    if endpoint_can_open(endpoint) {
        None
    } else if endpoint == "shared-local" {
        Some("Cannot attach queue-only route; owner address required")
    } else {
        Some("Cannot open route; explicit ws/wss/Unix owner endpoint required")
    }
}
fn route_identity(row: &Value, route: usize) -> Option<(&str, &str, &str)> {
    let binding = row["routes"]
        .as_array()
        .and_then(|routes| routes.get(route % routes.len().max(1)))?;
    Some((
        row["thread"].as_str()?,
        binding["name"].as_str()?,
        binding["endpoint"].as_str()?,
    ))
}
fn restore_selection(
    rows: &[Value],
    identity: Option<(&str, &str, &str)>,
    selection: &mut usize,
    route: &mut usize,
) {
    let Some((thread, name, endpoint)) = identity else {
        *selection = (*selection).min(rows.len().saturating_sub(1));
        *route = 0;
        return;
    };
    for (index, row) in rows.iter().enumerate() {
        if row["thread"] != thread {
            continue;
        }
        *selection = index;
        *route = row["routes"]
            .as_array()
            .and_then(|routes| {
                routes
                    .iter()
                    .position(|binding| binding["name"] == name && binding["endpoint"] == endpoint)
            })
            .unwrap_or(0);
        return;
    }
    *selection = (*selection).min(rows.len().saturating_sub(1));
    *route = 0;
}
fn route_dot(binding: &Value) -> &'static str {
    if binding["enabled"] != true || binding["removed"] == true {
        return "·";
    }
    let owner_state = binding["owner_health"]["state"]
        .as_str()
        .or_else(|| binding["owner_health"]["status"].as_str());
    match owner_state {
        Some("auth-required" | "unavailable" | "unloaded") => "○",
        Some("ready-to-receive") => "●",
        _ => "◐",
    }
}

fn conversation_dot(routes: &Value) -> &'static str {
    let dots: Vec<_> = routes
        .as_array()
        .into_iter()
        .flatten()
        .map(route_dot)
        .collect();
    for dot in ["○", "◐", "●"] {
        if dots.contains(&dot) {
            return dot;
        }
    }
    "·"
}

const MAX_DETAIL_ROUTES: usize = 5;

fn route_counts(routes: &Value) -> (usize, usize) {
    routes
        .as_array()
        .into_iter()
        .flatten()
        .filter(|binding| binding["removed"] != true)
        .fold((0, 0), |(active, paused), binding| {
            if binding["enabled"] == true {
                (active + 1, paused)
            } else {
                (active, paused + 1)
            }
        })
}

fn snapshot_route_counts(snapshot: &Value) -> (usize, usize) {
    route_counts(&snapshot["bindings"])
}

fn route_count_label(count: usize, label: &str) -> String {
    format!("{count} {label}{}", if count == 1 { "" } else { "s" })
}

fn route_state(binding: &Value) -> &'static str {
    if binding["removed"] == true {
        "removed"
    } else if binding["enabled"] == true {
        "active"
    } else {
        "paused"
    }
}

fn owner_state(binding: &Value) -> &str {
    binding["owner_health"]
        .get("status")
        .or_else(|| binding["owner_health"].get("state"))
        .and_then(Value::as_str)
        .unwrap_or("unverified")
}

fn route_sources(binding: &Value) -> String {
    let Some(sources) = binding["sources"].as_array() else {
        return "configured sources".into();
    };
    let mut names = sources
        .iter()
        .filter_map(Value::as_str)
        .take(4)
        .map(|source| clean(source, 20))
        .collect::<Vec<_>>();
    if sources.len() > names.len() {
        names.push("…".into());
    }
    if names.is_empty() {
        "configured sources".into()
    } else {
        names.join(", ")
    }
}

fn route_summary(routes: &Value, narrow: bool) -> String {
    let (active, paused) = route_counts(routes);
    if narrow {
        format!("{active}a/{paused}p")
    } else {
        format!(
            "{} · {}",
            route_count_label(active, "active route"),
            route_count_label(paused, "paused route")
        )
    }
}

fn groups(snapshot: &Value, filter: Option<&str>) -> Vec<Value> {
    let mut map: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for b in snapshot["bindings"].as_array().into_iter().flatten() {
        if b["removed"] == true {
            continue;
        }
        let id = b["thread"].as_str().unwrap_or("unknown");
        if filter.is_some_and(|f| f != id) {
            continue;
        }
        map.entry(id.into()).or_default().push(b.clone());
    }
    let mut rows = Vec::new();
    for (thread, routes) in map {
        let metadata = snapshot["conversations"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|v| v["thread"] == thread);
        let project = metadata
            .and_then(|v| v["project"].as_str())
            .unwrap_or("Ungrouped");
        let label = metadata
            .and_then(|v| v["display_name"].as_str())
            .filter(|v| !v.is_empty())
            .unwrap_or(&thread);
        rows.push(json!({"thread":thread,"project":project,"name":label,"routes":routes}));
    }
    let mut counts = BTreeMap::new();
    for r in &rows {
        *counts
            .entry((r["project"].to_string(), r["name"].to_string()))
            .or_insert(0usize) += 1;
    }
    for r in &mut rows {
        if counts[&(r["project"].to_string(), r["name"].to_string())] > 1 {
            r["name"] = json!(format!(
                "{} · {}",
                r["name"].as_str().unwrap_or(""),
                clean(r["thread"].as_str().unwrap_or(""), 8)
            ));
        }
    }
    rows.sort_by_key(|v| {
        (
            v["project"].as_str().unwrap_or("").to_owned(),
            v["name"].as_str().unwrap_or("").to_owned(),
            v["thread"].as_str().unwrap_or("").to_owned(),
        )
    });
    rows
}
#[allow(clippy::too_many_arguments)]
fn render(
    snapshot: &Value,
    rows: &[Value],
    selection: usize,
    route: usize,
    detail: bool,
    notice: &str,
    animate: bool,
    tick: bool,
) -> String {
    let (width, height) = terminal::size().unwrap_or((96, 24));
    render_sized(
        snapshot,
        rows,
        selection,
        route,
        detail,
        notice,
        animate,
        tick,
        width as usize,
        height as usize,
    )
}
#[allow(clippy::too_many_arguments)]
fn render_sized(
    snapshot: &Value,
    rows: &[Value],
    selection: usize,
    route: usize,
    detail: bool,
    notice: &str,
    _animate: bool,
    _tick: bool,
    width: usize,
    height: usize,
) -> String {
    let width = width.min(96);
    let narrow = width < 64;
    let (active_routes, paused_routes) = snapshot_route_counts(snapshot);
    let receiver = if snapshot["receiver"]["ready"] == true {
        "● ready"
    } else {
        "○ unavailable"
    };
    let mut lines = vec![
        if narrow {
            format!("  Auto-refresh on · health {receiver}")
        } else {
            "  codex-monitor · Rust · Auto-refresh on".into()
        },
        if narrow {
            format!(
                "  {} conv · {} active · {} paused",
                rows.len(),
                active_routes,
                paused_routes
            )
        } else {
            format!(
                "  {} conversations · {} · {} · Receiver health: {}",
                rows.len(),
                route_count_label(active_routes, "active route"),
                route_count_label(paused_routes, "paused route"),
                receiver
            )
        },
        String::new(),
    ];
    let selected_route_count = rows
        .get(selection)
        .and_then(|row| row["routes"].as_array())
        .map_or(0, Vec::len);
    let detail_lines = if detail {
        8 + selected_route_count.min(MAX_DETAIL_ROUTES)
            + 2 * usize::from(selected_route_count > MAX_DETAIL_ROUTES)
    } else {
        0
    };
    let visible = (height
        .saturating_sub(6 + detail_lines)
        .saturating_div(if detail { 2 } else { 1 }))
    .max(1);
    let start = selection
        .saturating_sub(visible / 2)
        .min(rows.len().saturating_sub(visible));
    let mut project = String::new();
    for (i, r) in rows.iter().enumerate().skip(start).take(visible) {
        let group = r["project"].as_str().unwrap_or("Ungrouped");
        if project != group {
            lines.push(format!("  {}", clean(group, 60)));
            project = group.into();
        }
        let summary = route_summary(&r["routes"], narrow);
        let name_width = if narrow {
            width.saturating_sub(summary.len() + 8).clamp(10, 30)
        } else {
            30
        };
        lines.push(format!(
            "{} {} {}  {}",
            if i == selection { "›" } else { " " },
            conversation_dot(&r["routes"]),
            padded(r["name"].as_str().unwrap_or(""), name_width),
            summary
        ));
    }
    if let Some(row) = rows.get(selection)
        && let Some(b) = row["routes"]
            .as_array()
            .and_then(|v| v.get(route % v.len().max(1)))
    {
        lines.push(String::new());
        lines.push(format!(
            "  Route: {}  {}",
            clean(b["name"].as_str().unwrap_or(""), 50),
            route_dot(b)
        ));
        if detail {
            lines.push(format!(
                "  Thread: {}",
                row["thread"].as_str().unwrap_or("")
            ));
            lines.push(format!(
                "  Purpose: receive {} → {}",
                route_sources(b),
                row["thread"].as_str().unwrap_or("")
            ));
            lines.push(format!(
                "  Status: {} · owner {}{}",
                route_state(b),
                owner_state(b),
                b["owner_health"]
                    .get("reason")
                    .and_then(Value::as_str)
                    .map(|reason| format!(" · {}", clean(reason, 50)))
                    .unwrap_or_default()
            ));
            lines.push(format!(
                "  Endpoint: {}",
                b["endpoint"].as_str().unwrap_or("unavailable")
            ));
            lines.push(format!(
                "  Open: {}",
                enter_hint(b["endpoint"].as_str().unwrap_or(""))
            ));
            let routes = row["routes"].as_array().cloned().unwrap_or_default();
            let selected_route = route % routes.len().max(1);
            let route_start = selected_route
                .saturating_sub(MAX_DETAIL_ROUTES / 2)
                .min(routes.len().saturating_sub(MAX_DETAIL_ROUTES));
            let route_end = (route_start + MAX_DETAIL_ROUTES).min(routes.len());
            lines.push("  Routes (Tab selects):".into());
            if route_start > 0 {
                lines.push(format!(
                    "    … {} earlier route{}",
                    route_start,
                    if route_start == 1 { "" } else { "s" }
                ));
            }
            for (index, binding) in routes
                .iter()
                .enumerate()
                .skip(route_start)
                .take(MAX_DETAIL_ROUTES)
            {
                lines.push(format!(
                    "    {}{} {} · {} · {}",
                    if index == selected_route {
                        "› "
                    } else {
                        "  "
                    },
                    route_dot(binding),
                    clean(binding["name"].as_str().unwrap_or("unnamed"), 24),
                    route_state(binding),
                    owner_state(binding)
                ));
            }
            if route_end < routes.len() {
                lines.push(format!(
                    "    … {} more route{}",
                    routes.len() - route_end,
                    if routes.len() - route_end == 1 {
                        ""
                    } else {
                        "s"
                    }
                ));
            }
            lines.push("  Model execution: unverified · no live model/tool telemetry".into());
            lines.push("  Scope: delivery state; no live model/tool telemetry".into());
        }
    }
    let selected_enter_hint = rows
        .get(selection)
        .and_then(|row| row["routes"].as_array())
        .and_then(|routes| routes.get(route % routes.len().max(1)))
        .and_then(|binding| binding["endpoint"].as_str())
        .map(enter_hint)
        .unwrap_or("Enter unavailable; no route");
    lines.push(format!("  {}", selected_enter_hint));
    lines.push(format!("  {}", clean(notice, 90)));
    lines.push("  ↑↓ select · Tab/[ ] route · p pause · r resume · x remove".into());
    lines.push("  d details · q quit".into());
    lines
        .into_iter()
        .take(height.max(1))
        .map(|s| clean(&s, width))
        .collect::<Vec<_>>()
        .join("\r\n")
}
struct TerminalGuard;
impl TerminalGuard {
    fn enter() -> Result<Self> {
        terminal::enable_raw_mode()?;
        execute!(io::stdout(), terminal::EnterAlternateScreen, cursor::Hide)?;
        Ok(Self)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
        let _ = execute!(io::stdout(), cursor::Show, terminal::LeaveAlternateScreen);
    }
}
#[allow(clippy::too_many_arguments)]
pub async fn dashboard(
    store: Store,
    cfg: Config,
    root: PathBuf,
    pool: SessionPool,
    once: bool,
    as_json: bool,
    thread: Option<String>,
    interval: Duration,
    color: String,
    no_animate: bool,
) -> Result<()> {
    let mut snapshot = runtime::snapshot(&store, &cfg, &root, &pool).await?;
    let mut rows = groups(&snapshot, thread.as_deref());
    if once {
        if as_json {
            snapshot["connections"] = json!(rows);
            output(snapshot);
        } else {
            println!(
                "{}",
                render(
                    &snapshot,
                    &rows,
                    0,
                    0,
                    false,
                    "Dots show configuration, not model activity",
                    false,
                    false
                )
            );
        }
        return Ok(());
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        anyhow::bail!("live dashboard needs a terminal; use --once --json");
    }
    let mut guard = Some(TerminalGuard::enter()?);
    let mut selection = 0usize;
    let mut route = 0usize;
    let mut detail = false;
    let mut notice = String::new();
    let mut deleting: Option<String> = None;
    let mut selection_valid = true;
    let mut refresh = Instant::now();
    let mut tick = false;
    let mut previous_frame = String::new();
    loop {
        if refresh.elapsed() >= interval {
            let previous_selection = rows
                .get(selection)
                .and_then(|row| route_identity(row, route))
                .map(|(thread, name, endpoint)| {
                    (thread.to_owned(), name.to_owned(), endpoint.to_owned())
                });
            snapshot = runtime::snapshot(&store, &cfg, &root, &pool).await?;
            rows = groups(&snapshot, thread.as_deref());
            restore_selection(
                &rows,
                previous_selection.as_ref().map(|(thread, name, endpoint)| {
                    (thread.as_str(), name.as_str(), endpoint.as_str())
                }),
                &mut selection,
                &mut route,
            );
            let restored = rows
                .get(selection)
                .and_then(|row| route_identity(row, route));
            if previous_selection
                .as_ref()
                .is_some_and(|(t, n, e)| restored != Some((t.as_str(), n.as_str(), e.as_str())))
            {
                selection_valid = false;
                deleting = None;
                notice = "Selected route changed; use arrows or Tab to select again".into();
            }
            refresh = Instant::now();
        }
        let frame = render(
            &snapshot,
            &rows,
            selection,
            route,
            detail,
            &notice,
            !no_animate,
            tick,
        );
        if frame != previous_frame {
            execute!(io::stdout(), cursor::MoveTo(0, 0))?;
            let displayed = frame.replace("\r\n", "\x1b[K\r\n") + "\x1b[K";
            let colored = color != "never" && std::env::var_os("NO_COLOR").is_none();
            if colored {
                print!(
                    "{}",
                    displayed
                        .replace('●', "\x1b[32m●\x1b[0m")
                        .replace('○', "\x1b[31m○\x1b[0m")
                        .replace('◐', "\x1b[33m◐\x1b[0m")
                );
            } else {
                print!("{displayed}");
            }
            execute!(io::stdout(), terminal::Clear(ClearType::FromCursorDown))?;
            io::stdout().flush()?;
            previous_frame = frame;
        }
        tick = !tick;
        if !event::poll(Duration::from_millis(250))? {
            continue;
        }
        let Event::Key(key) = event::read()? else {
            continue;
        };
        if key.kind != KeyEventKind::Press {
            continue;
        }
        if key.code == KeyCode::Char('q')
            || (key.code == KeyCode::Char('c')
                && key.modifiers.contains(event::KeyModifiers::CONTROL))
        {
            break;
        }
        if let Some(name) = deleting.take() {
            if key.code == KeyCode::Char('y') {
                notice = match store.route_action(&name, "remove") {
                    Ok(_) => "Route removed; conversation and receipts preserved".into(),
                    Err(e) => e.to_string(),
                };
                refresh = Instant::now() - interval;
            } else {
                notice = "Removal cancelled".into();
            }
            continue;
        }
        match key.code {
            KeyCode::Down | KeyCode::Char('j') => {
                notice.clear();
                selection_valid = true;
                selection = (selection + 1).min(rows.len().saturating_sub(1));
                route = 0;
            }
            KeyCode::Up | KeyCode::Char('k') => {
                notice.clear();
                selection_valid = true;
                selection = selection.saturating_sub(1);
                route = 0;
            }
            KeyCode::Tab | KeyCode::Char(']') | KeyCode::Char('[') => {
                notice.clear();
                selection_valid = true;
                if key.code == KeyCode::Char('[') {
                    route = route.wrapping_sub(1);
                } else {
                    route = route.wrapping_add(1);
                }
            }
            KeyCode::Char('d') => detail = !detail,
            KeyCode::Char('p') | KeyCode::Char('r') | KeyCode::Char('x') | KeyCode::Enter => {
                if !selection_valid {
                    continue;
                }
                let Some(row) = rows.get(selection) else {
                    continue;
                };
                let Some(routes) = row["routes"].as_array() else {
                    continue;
                };
                let Some(b) = routes.get(route % routes.len().max(1)) else {
                    continue;
                };
                let name = text(b, "name")?;
                if key.code == KeyCode::Char('x') {
                    deleting = Some(name.into());
                    notice = format!(
                        "Remove route {}? y confirms; any other key cancels",
                        clean(name, 40)
                    );
                    continue;
                }
                if key.code == KeyCode::Enter {
                    if let Some(message) = enter_notice(text(b, "endpoint")?) {
                        notice = message.into();
                        continue;
                    }
                    let current = store.bindings()?;
                    let found = current.as_array().context("routes")?.iter().find(|v| {
                        v["name"] == b["name"]
                            && v["thread"] == b["thread"]
                            && v["endpoint"] == b["endpoint"]
                            && v["removed"] != true
                    });
                    if found.is_none() {
                        notice = "Route changed; refresh and select again".into();
                        continue;
                    }
                    drop(guard.take());
                    let result =
                        connect(text(b, "endpoint")?, Some(text(b, "thread")?), None).await;
                    guard = Some(TerminalGuard::enter()?);
                    previous_frame.clear();
                    notice = result
                        .err()
                        .map_or("Returned from TUI".into(), |e| e.to_string());
                } else {
                    notice = match store.route_action(
                        name,
                        if key.code == KeyCode::Char('p') {
                            "pause"
                        } else {
                            "resume"
                        },
                    ) {
                        Ok(_) => "Route updated; accepted native input is unchanged".into(),
                        Err(e) => e.to_string(),
                    };
                }
                refresh = Instant::now() - interval;
            }
            _ => {}
        }
    }
    drop(guard);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn snapshot(endpoint: &str) -> Value {
        json!({
            "bindings": [{
                "name": "route",
                "thread": "thread",
                "endpoint": endpoint,
                "enabled": true,
                "removed": false
            }],
            "conversations": [],
            "receiver": {"ready": false}
        })
    }

    fn rendered_hint(endpoint: &str, width: usize) -> String {
        let snapshot = snapshot(endpoint);
        let rows = groups(&snapshot, None);
        render_sized(&snapshot, &rows, 0, 0, false, "", false, false, width, 24)
    }

    #[test]
    fn enabled_routes_do_not_imply_healthy_conversations() {
        let ready = json!({"enabled":true,"owner_health":{"state":"ready-to-receive"}});
        let unknown = json!({"enabled":true});
        let failed = json!({"enabled":true,"owner_health":{"state":"auth-required"}});
        let paused = json!({"enabled":false});
        assert_eq!(route_dot(&ready), "●");
        assert_eq!(route_dot(&unknown), "◐");
        assert_eq!(route_dot(&failed), "○");
        assert_eq!(route_dot(&paused), "·");
        assert_eq!(conversation_dot(&json!([ready, unknown, failed])), "○");
        assert_eq!(conversation_dot(&json!([paused])), "·");
    }

    #[test]
    fn shared_local_route_explains_that_enter_needs_an_owner() {
        let rendered = rendered_hint("shared-local", 40);

        assert!(rendered.contains("Cannot attach; owner address needed"));
        assert!(!rendered.contains("Enter open"));
        assert_eq!(
            enter_notice("shared-local"),
            Some("Cannot attach queue-only route; owner address required")
        );
    }

    #[test]
    fn explicit_owner_route_keeps_enter_open_affordance() {
        let rendered = rendered_hint("ws://127.0.0.1:4500", 40);

        assert!(rendered.contains("Enter open"));
        assert!(!rendered.contains("owner address required"));
        assert_eq!(enter_notice("ws://127.0.0.1:4500"), None);
    }

    #[test]
    fn unsupported_route_does_not_claim_to_open() {
        let rendered = rendered_hint("ssh://owner", 40);

        assert!(rendered.contains("Cannot open; owner endpoint needed"));
        assert!(!rendered.contains("Enter open"));
        assert_eq!(
            enter_notice("ssh://owner"),
            Some("Cannot open route; explicit ws/wss/Unix owner endpoint required")
        );
    }

    #[test]
    fn detail_view_shows_owner_health_without_claiming_model_readiness() {
        let snapshot = json!({
            "bindings": [{
                "name": "route",
                "thread": "thread",
                "endpoint": "wss://owner.example",
                "enabled": true,
                "removed": false,
                "owner_health": {
                    "status": "auth-required",
                    "reason": "authentication required",
                    "model_execution": "unverified"
                }
            }],
            "conversations": [],
            "receiver": {"ready": false}
        });
        let rows = groups(&snapshot, None);
        let rendered = render_sized(&snapshot, &rows, 0, 0, true, "", false, false, 96, 24);

        assert!(rendered.contains("Purpose: receive configured sources → thread"));
        assert!(rendered.contains("Status: active · owner auth-required"));
        assert!(rendered.contains("authentication required"));
        assert!(rendered.contains("Endpoint: wss://owner.example"));
        assert!(rendered.contains("Open: Enter open"));
        assert!(rendered.contains("Routes (Tab selects):"));
        assert!(rendered.contains("Model execution: unverified"));
    }

    #[test]
    fn header_counts_active_and_paused_routes_and_labels_health_separately() {
        let snapshot = json!({
            "bindings": [
                {"name":"active-one","thread":"one","enabled":true,"removed":false},
                {"name":"active-two","thread":"two","enabled":true,"removed":false},
                {"name":"paused","thread":"three","enabled":false,"removed":false},
                {"name":"retired","thread":"four","enabled":true,"removed":true}
            ],
            "conversations": [],
            "receiver": {"ready": true}
        });
        let rows = groups(&snapshot, None);
        let rendered = render_sized(&snapshot, &rows, 0, 0, false, "", true, true, 96, 24);

        assert!(rendered.contains("Auto-refresh on"));
        assert!(rendered.contains("2 active routes"));
        assert!(rendered.contains("1 paused route"));
        assert!(rendered.contains("Receiver health: ● ready"));
        assert!(!rendered.contains("connections"));
    }

    #[test]
    fn detail_view_lists_routes_and_marks_the_selected_route() {
        let snapshot = json!({
            "bindings": [
                {
                    "name":"queue",
                    "thread":"thread",
                    "endpoint":"shared-local",
                    "sources":["webhook"],
                    "enabled":false,
                    "removed":false,
                    "owner_health":{"status":"unverified"}
                },
                {
                    "name":"owner",
                    "thread":"thread",
                    "endpoint":"ws://127.0.0.1:4500",
                    "sources":["manual"],
                    "enabled":true,
                    "removed":false,
                    "owner_health":{"status":"ready-to-receive"}
                }
            ],
            "conversations": [],
            "receiver": {"ready": false}
        });
        let rows = groups(&snapshot, None);
        let rendered = render_sized(&snapshot, &rows, 0, 1, true, "", false, false, 96, 32);

        assert!(rendered.contains("Routes (Tab selects):"));
        assert!(rendered.contains("queue · paused · unverified"));
        assert!(rendered.contains("› ● owner · active · ready-to-receive"));
        assert!(rendered.contains("Purpose: receive manual → thread"));
        assert!(rendered.contains("Open: Enter open"));
    }

    #[test]
    fn refresh_restores_the_selected_route_identity_after_reordering() {
        let first = json!({
            "thread": "thread",
            "routes": [
                {"name": "queue", "endpoint": "shared-local"},
                {"name": "owner", "endpoint": "ws://127.0.0.1:4500"}
            ]
        });
        let second = json!({
            "thread": "thread",
            "routes": [
                {"name": "owner", "endpoint": "ws://127.0.0.1:4500"},
                {"name": "queue", "endpoint": "shared-local"}
            ]
        });
        let identity = route_identity(&first, 1);
        let mut selection = 0;
        let mut route = 1;

        restore_selection(
            std::slice::from_ref(&second),
            identity,
            &mut selection,
            &mut route,
        );

        assert_eq!(selection, 0);
        assert_eq!(route, 0);
        assert_eq!(route_identity(&second, route), identity);
    }
}
