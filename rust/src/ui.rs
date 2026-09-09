use crate::{Config, connect, output, runtime, text};
use anyhow::{Context, Result};
use codex_monitor_rs::store::Store;
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
    let width = (width as usize).min(96);
    let height = height as usize;
    let mut lines = vec![
        format!(
            "  codex-monitor   {} live   ·   Rust",
            if animate && tick { "●" } else { "·" }
        ),
        format!(
            "  {} conversations   ·   receiver {}",
            rows.len(),
            if snapshot["receiver"]["ready"] == true {
                "●"
            } else {
                "○"
            }
        ),
        String::new(),
    ];
    let visible = (height.saturating_sub(if detail { 12 } else { 9 }) / 2).max(1);
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
        let active = r["routes"]
            .as_array()
            .is_some_and(|v| v.iter().any(|b| b["enabled"] == true));
        lines.push(format!(
            "{} {} {}  {} routes",
            if i == selection { "›" } else { " " },
            if active { "●" } else { "○" },
            padded(r["name"].as_str().unwrap_or(""), 30),
            r["routes"].as_array().map_or(0, Vec::len)
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
            if b["enabled"] == true { "●" } else { "○" }
        ));
        if detail {
            lines.push(format!(
                "  Thread: {}",
                row["thread"].as_str().unwrap_or("")
            ));
            lines.push(format!("  Owner: {}", b["endpoint"].as_str().unwrap_or("")));
            lines.push("  Scope: delivery state; no live model/tool telemetry".into());
        }
    }
    lines.push(format!("  {}", clean(notice, 90)));
    lines.push("  ↑↓ select · Tab route · Enter open · p pause · r resume · x remove".into());
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
    once: bool,
    as_json: bool,
    thread: Option<String>,
    interval: Duration,
    color: String,
    no_animate: bool,
) -> Result<()> {
    let mut snapshot = runtime::snapshot(&store, &cfg, &root).await?;
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
    let mut refresh = Instant::now();
    let mut tick = false;
    let mut previous_frame = String::new();
    loop {
        if refresh.elapsed() >= interval {
            snapshot = runtime::snapshot(&store, &cfg, &root).await?;
            rows = groups(&snapshot, thread.as_deref());
            selection = selection.min(rows.len().saturating_sub(1));
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
                selection = (selection + 1).min(rows.len().saturating_sub(1));
                route = 0;
            }
            KeyCode::Up | KeyCode::Char('k') => {
                selection = selection.saturating_sub(1);
                route = 0;
            }
            KeyCode::Tab => route = route.wrapping_add(1),
            KeyCode::Char('d') => detail = !detail,
            KeyCode::Char('p') | KeyCode::Char('r') | KeyCode::Char('x') | KeyCode::Enter => {
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
