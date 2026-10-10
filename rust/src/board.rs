//! Width-aware project cards and explicit shared-server actions.
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use unicode_width::UnicodeWidthChar;
#[derive(Clone, Debug, PartialEq)]
pub enum Hit {
    Conversation(usize),
    Action(usize),
}
pub struct Board {
    pub width: usize,
    pub height: usize,
    cells: Vec<Vec<String>>,
    pub hits: BTreeMap<(usize, usize), Hit>,
}
impl Board {
    fn new(w: usize, h: usize) -> Self {
        Self {
            width: w,
            height: h,
            cells: vec![vec![" ".into(); w]; h],
            hits: BTreeMap::new(),
        }
    }
    fn text(&mut self, x: usize, y: usize, s: &str, max: usize) {
        if y >= self.height {
            return;
        }
        let mut col = x;
        for c in s.chars().filter(|c| !c.is_control()) {
            let n = c.width().unwrap_or(0);
            if n == 0 {
                continue;
            }
            if col + n > self.width || col + n > x + max {
                break;
            }
            self.cells[y][col] = c.to_string();
            for i in 1..n {
                self.cells[y][col + i] = String::new();
            }
            col += n;
        }
    }
    fn line(&mut self, x: usize, y: usize, w: usize, s: &str) {
        self.text(x, y, s, w);
    }
    fn box_at(&mut self, x: usize, y: usize, w: usize, h: usize, title: &str) {
        if w < 2 || h < 2 {
            return;
        }
        self.text(x, y, &format!("╭{}╮", "─".repeat(w - 2)), w);
        self.text(x, y + h - 1, &format!("╰{}╯", "─".repeat(w - 2)), w);
        for r in y + 1..y + h - 1 {
            self.text(x, r, "│", 1);
            self.text(x + w - 1, r, "│", 1);
        }
        self.text(x + 2, y, title, w.saturating_sub(4));
    }
    fn hit(&mut self, x: usize, y: usize, w: usize, target: Hit) {
        if y < self.height {
            for c in x..(x + w).min(self.width) {
                self.hits.insert((c, y), target.clone());
            }
        }
    }
    pub fn render(&self) -> String {
        self.cells
            .iter()
            .map(|row| row.concat().trim_end().to_owned())
            .collect::<Vec<_>>()
            .join("\r\n")
    }
}
pub fn permission(row: &Value) -> String {
    let all = row["routes"].as_array().cloned().unwrap_or_default();
    let active: Vec<_> = all.iter().filter(|b| b["enabled"] == true).collect();
    let labels: BTreeSet<_> = if active.is_empty() {
        all.iter().collect::<Vec<_>>()
    } else {
        active
    }
    .iter()
    .map(|b| {
        b["permission"]["label"]
            .as_str()
            .unwrap_or("Access unknown")
    })
    .collect();
    if labels.len() == 1 {
        labels.first().unwrap().to_string()
    } else if labels.is_empty() {
        "Access unknown".into()
    } else {
        "Check permissions".into()
    }
}
fn ready(row: &Value) -> &'static str {
    let routes = row["routes"].as_array().cloned().unwrap_or_default();
    let active: Vec<_> = routes.iter().filter(|b| b["enabled"] == true).collect();
    if active.is_empty() {
        "Paused"
    } else if active
        .iter()
        .any(|b| b["owner_health"]["status"] == "auth-required")
    {
        "Login required"
    } else if active
        .iter()
        .any(|b| b["owner_health"]["status"] == "execution-error")
    {
        "Execution error"
    } else if active
        .iter()
        .any(|b| b["owner_health"]["status"] == "waiting-for-approval")
    {
        "Approval needed"
    } else if active
        .iter()
        .any(|b| b["owner_health"]["status"] == "waiting-for-user-input")
    {
        "Response needed"
    } else if active.iter().all(|b| b["owner_health"]["ready"] == true) {
        "Ready"
    } else {
        "Needs review"
    }
}
pub fn preferred(row: &Value) -> usize {
    row["routes"]
        .as_array()
        .and_then(|a| a.iter().position(|b| b["enabled"] == true))
        .unwrap_or(0)
}
fn age(seconds: f64) -> String {
    let seconds = seconds.max(0.0) as u64;
    if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3600 {
        format!("{}m", seconds / 60)
    } else if seconds < 86400 {
        format!("{}h {}m", seconds / 3600, seconds % 3600 / 60)
    } else {
        format!("{}d {}h", seconds / 86400, seconds % 86400 / 3600)
    }
}
fn last_seen(row: &Value) -> String {
    let timestamp = row["routes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|b| b["events"]["latest"]["updated"].as_f64())
        .reduce(f64::max);
    timestamp
        .map(|t| format!("{} ago", age(crate::now() - t)))
        .unwrap_or("No delivery recorded".into())
}

fn wrap(s: &str, w: usize) -> Vec<String> {
    let mut out = Vec::new();
    for line in s.lines() {
        let mut row = String::new();
        let mut used = 0;
        for word in line.split_whitespace() {
            let n = word.chars().map(|c| c.width().unwrap_or(0)).sum::<usize>();
            if used > 0 && used + n + 1 > w {
                out.push(row);
                row = String::new();
                used = 0;
            }
            if used > 0 {
                row.push(' ');
                used += 1;
            }
            for c in word.chars() {
                let size = c.width().unwrap_or(0);
                if used + size > w && used > 0 {
                    out.push(std::mem::take(&mut row));
                    used = 0;
                }
                row.push(c);
                used += size;
            }
        }
        out.push(row);
    }
    out
}
pub fn labels(menu: bool, enabled: bool) -> Vec<&'static str> {
    if menu {
        vec!["Full Access", "Read-only", "Project Access", "Back"]
    } else {
        vec![
            "Open in Codex",
            if enabled { "Pause" } else { "Resume" },
            "Remove",
            "Reconnect",
            "Change Permission",
            "Close",
        ]
    }
}
#[allow(clippy::too_many_arguments)]
pub fn build(
    snapshot: &Value,
    rows: &[Value],
    selected: usize,
    route: usize,
    detail: bool,
    menu: bool,
    action: usize,
    scroll: usize,
    notice: &str,
    width: usize,
    height: usize,
) -> Board {
    let w = width.max(24);
    let h = height.max(8);
    let mut b = Board::new(w, h);
    b.text(
        1,
        0,
        if snapshot["dashboard_update_available"] == true {
            "UPDATE INSTALLED · q then reopen codex-monitor dashboard"
        } else {
            "CODEX MONITOR · Rust"
        },
        w - 2,
    );
    let failures = rows
        .iter()
        .filter(|r| ready(r) == "Execution error" || ready(r) == "Login required")
        .count();
    let pending: u64 = rows
        .iter()
        .flat_map(|r| r["routes"].as_array().into_iter().flatten())
        .filter(|r| r["enabled"] == true)
        .map(|r| r["events"]["counts"]["pending"].as_u64().unwrap_or(0))
        .sum();
    let unchecked = rows.iter().filter(|r| ready(r) == "Needs review").count();
    let decisions = rows
        .iter()
        .filter(|r| ["Approval needed", "Response needed"].contains(&ready(r)))
        .count();
    let mut top = 3usize;
    if w >= 76 && h >= 20 {
        let metrics = [
            (
                "RECEIVER",
                if snapshot["receiver"]["ready"] == true {
                    "Online".into()
                } else {
                    "Unavailable".into()
                },
            ),
            (
                "ATTENTION",
                format!("{decisions} waiting · {failures} failed"),
            ),
            ("DELIVERY", format!("{pending} pending")),
            ("VERIFICATION", format!("{unchecked} unchecked")),
        ];
        for (i, (title, value)) in metrics.iter().enumerate() {
            let step = w / 4;
            b.box_at(i * step, 2, step, 3, title);
            b.text(i * step + 2, 3, value, step - 4);
        }
        top = 6;
    } else {
        b.text(
            1,
            1,
            &format!(
                "{} conversations · {decisions} waiting · {failures} failed",
                rows.len()
            ),
            w - 2,
        );
    }
    let resources = &snapshot["resources"];
    if resources["process"]["available"] == true {
        b.text(
            1,
            top,
            &format!(
                "Monitor {:.1} MiB · {:.1}% CPU · {} processes (receiver tree)",
                resources["process"]["rss_bytes"].as_f64().unwrap_or(0.0) / 1048576.0,
                resources["process"]["cpu_percent"].as_f64().unwrap_or(0.0),
                resources["process"]["processes"]
            ),
            w - 2,
        );
        top += 1;
    }
    if resources["process"]["consecutive_zombie_samples"]
        .as_u64()
        .unwrap_or(0)
        >= 2
    {
        b.text(
            1,
            top,
            "RESOURCE ALERT: persistent zombies; inspect sampler reaping",
            w - 2,
        );
        top += 1;
    }
    if h >= 32 {
        let oldest = rows
            .iter()
            .flat_map(|r| r["routes"].as_array().into_iter().flatten())
            .filter(|r| r["enabled"] == true)
            .filter_map(|r| r["events"]["oldest_unresolved_age_seconds"].as_f64())
            .reduce(f64::max);
        b.text(
            1,
            top,
            &format!(
                "Oldest unresolved: {} · queue acceptance is not a completed reply",
                oldest.map(age).unwrap_or("None".into())
            ),
            w - 2,
        );
        top += 1;
    }
    let issues: Vec<_> = rows
        .iter()
        .enumerate()
        .filter(|(_, r)| {
            [
                "Execution error",
                "Login required",
                "Approval needed",
                "Response needed",
                "Needs review",
            ]
            .contains(&ready(r))
        })
        .take(4)
        .collect();
    if !issues.is_empty() && h >= 20 {
        let count = issues.len().min(2);
        b.box_at(0, top, w, count + 2, "NEEDS ATTENTION");
        for (n, (i, r)) in issues.iter().take(count).enumerate() {
            b.text(
                2,
                top + 1 + n,
                &format!(
                    "{} / {} · {}",
                    r["project"].as_str().unwrap_or(""),
                    r["name"].as_str().unwrap_or(""),
                    ready(r)
                ),
                w - 4,
            );
            b.hit(1, top + 1 + n, w - 2, Hit::Conversation(*i));
        }
        top += count + 3;
    }
    let mut groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, r) in rows.iter().enumerate() {
        groups
            .entry(r["project"].as_str().unwrap_or("Ungrouped").into())
            .or_default()
            .push(i);
    }
    let columns = (w / 55).clamp(1, 3);
    let cardw = w / columns;
    let mut ys = vec![top; columns];
    let mut panels = Vec::new();
    let mut selected_y = top;
    for (project, indices) in groups {
        let col = (0..columns).min_by_key(|c| ys[*c]).unwrap();
        let y = ys[col];
        let height = indices.len() * 2 + 2;
        for (n, i) in indices.iter().enumerate() {
            if *i == selected {
                selected_y = y + 1 + n * 2;
            }
        }
        panels.push((col * cardw, y, cardw, height, project, indices));
        ys[col] += height + 1;
    }
    let offset = selected_y.saturating_sub(h.saturating_sub(4));
    for (x, y, cw, ch, project, indices) in panels {
        let shifted = y as isize - offset as isize;
        if shifted >= top as isize {
            b.box_at(
                x,
                shifted as usize,
                cw,
                ch,
                &format!("{} · {} conversations", project, indices.len()),
            );
        }
        for (n, i) in indices.iter().enumerate() {
            let py = shifted + 1 + (n * 2) as isize;
            if py < top as isize || py as usize >= h - 1 {
                continue;
            }
            let py = py as usize;
            let r = &rows[*i];
            let state = ready(r);
            let namew = cw.saturating_sub(21);
            b.text(
                x + 2,
                py,
                &format!(
                    "{} {}",
                    if *i == selected { "›" } else { " " },
                    r["name"].as_str().unwrap_or("")
                ),
                namew,
            );
            b.text(x + cw.saturating_sub(18), py, state, 16);
            b.text(
                x + 4,
                py + 1,
                &format!("Saved: {} · {}", permission(r), last_seen(r)),
                cw.saturating_sub(6),
            );
            b.hit(x + 1, py, cw - 2, Hit::Conversation(*i));
            b.hit(x + 1, py + 1, cw - 2, Hit::Conversation(*i));
        }
    }
    b.line(0, h - 1, w, "↑↓ select · Enter details · q quit");
    if !detail {
        if !notice.is_empty() {
            b.line(1, h - 2, w - 2, notice);
        }
        return b;
    }
    let Some(row) = rows.get(selected) else {
        return b;
    };
    let routes = row["routes"].as_array().cloned().unwrap_or_default();
    let binding = routes
        .get(route % routes.len().max(1))
        .cloned()
        .unwrap_or(json!({}));
    let dw = w.min(112);
    let dh = h.min(26);
    let x = (w - dw) / 2;
    let y = (h - dh) / 2;
    for r in y..y + dh {
        for c in x..x + dw {
            b.cells[r][c] = " ".into();
        }
    }
    b.hits.clear();
    b.box_at(
        x,
        y,
        dw,
        dh,
        &format!(
            "{} / {}",
            row["project"].as_str().unwrap_or(""),
            row["name"].as_str().unwrap_or("")
        ),
    );
    let mut labels = labels(menu, binding["enabled"] == true);
    if !menu {
        labels[4] = if binding["permission"]["scope"] == "project" {
            "Project permissions"
        } else {
            "Server permissions"
        };
    }
    let mut buttons: Vec<Vec<(usize, &str, usize)>> = vec![Vec::new()];
    let mut used = 0;
    for (i, label) in labels.iter().enumerate() {
        let size = (label.len() + 4).min(dw - 4);
        if used > 0 && used + size + 1 > dw - 4 {
            buttons.push(Vec::new());
            used = 0;
        }
        buttons.last_mut().unwrap().push((i, label, size));
        used += size + usize::from(used > 0);
    }
    let button_top = y + dh - 2 - buttons.len();
    let mut lines = vec![
        ready(row).into(),
        format!(
            "Saved permissions (server/resident defaults): {}",
            binding["permission"]["label"]
                .as_str()
                .unwrap_or("Access unknown")
        ),
        "Saved service settings; current runtime permissions are not verified by this label."
            .into(),
        format!(
            "Server: {}",
            binding["endpoint"].as_str().unwrap_or("unknown")
        ),
        format!(
            "Connection {}/{}: {} ({})",
            route % routes.len().max(1) + 1,
            routes.len(),
            binding["name"].as_str().unwrap_or(""),
            if binding["enabled"] == true {
                "enabled"
            } else {
                "paused"
            }
        ),
    ];
    if binding["owner_health"]["attention"]["status"] == "required" {
        lines.insert(
            1,
            binding["owner_health"]["reason"]
                .as_str()
                .unwrap_or("Open in Codex to review and respond.")
                .to_owned(),
        );
    }
    if binding["owner_health"]["status"] == "execution-error" {
        lines.insert(
            1,
            format!(
                "EXECUTION ERROR: {}",
                binding["owner_health"]["execution_error"]["message"]
                    .as_str()
                    .unwrap_or("Recent error details unavailable. Open in Codex to inspect.")
            ),
        );
    }
    if menu {
        lines=vec!["CHANGE SHARED SERVER PERMISSIONS".into(),format!("Server: {}",binding["endpoint"].as_str().unwrap_or("unknown")),"This changes ALL conversations using this server, including other projects. It does not change only this conversation.".into(),"Full Access: unrestricted filesystem and command networking. Approval policy and OS sudo privileges stay unchanged.".into(),"Read-only: no filesystem writes or command networking.".into(),"Project Access: workspace writes and internet; configured writable roots are preserved.".into(),"Settings persist until changed. Choose a mode to preview affected conversations; choose the same mode again to apply.".into()];
        lines.insert(
            1,
            crate::reconnect::permission_scope(&binding["permission"]),
        );
    } else {
        lines.extend(["".into(),"Reconnect reloads the current login for this shared server. It preserves permissions and does not replay failed input.".into(),"Permission changes apply to the entire shared server. Inspect the affected conversation list before confirming.".into(),"Project Access allows project writes and internet access; external setup paths remain restricted.".into()]);
    }
    if !notice.is_empty() {
        lines.splice(0..0, [notice.to_owned(), String::new()]);
    }
    let lines = lines
        .iter()
        .flat_map(|l| wrap(l, dw - 6))
        .collect::<Vec<_>>();
    let slots = button_top.saturating_sub(y + 3);
    for (n, line) in lines
        .iter()
        .skip(scroll.min(lines.len().saturating_sub(slots)))
        .take(slots)
        .enumerate()
    {
        b.text(x + 3, y + 1 + n, line, dw - 6);
    }
    if lines.len() > slots {
        b.text(
            x + 3,
            button_top - 1,
            "↑↓ scroll details · Tab next connection",
            dw - 6,
        );
    }
    for (n, buttons) in buttons.iter().enumerate() {
        let mut bx = x + 2;
        for (i, label, size) in buttons {
            b.text(
                bx,
                button_top + n,
                &format!(
                    "{} {} {}",
                    if *i == action { "▶" } else { "[" },
                    label,
                    if *i == action { "◀" } else { "]" }
                ),
                *size,
            );
            b.hit(bx, button_top + n, *size, Hit::Action(*i));
            bx += size + 1;
        }
    }
    b.text(
        x + 2,
        y + dh - 2,
        "←→ action · Enter select · Esc back",
        dw - 4,
    );
    b
}
#[cfg(test)]
mod tests {
    use super::*;
    fn rows() -> Vec<Value> {
        vec![
            json!({"thread":"t","name":"PM","project":"Project","routes":[{"name":"b","enabled":true,"endpoint":"ws://127.0.0.1:1","permission":{"label":"Project Access"}}]}),
        ]
    }
    #[test]
    fn narrow_preview_preserves_complete_identifiers() {
        let id = "00000000-1111-4222-8333-444444444444";
        let lines = wrap(id, 20);
        assert_eq!(lines.concat(), id);
        assert!(lines.iter().all(|line| line.len() <= 20));
    }
    #[test]
    fn wide_buttons_stay_in_one_row_and_narrow_buttons_remain_visible() {
        for w in [40, 60, 80, 100, 120, 160] {
            let b = build(&json!({}), &rows(), 0, 0, true, false, 0, 0, "", w, 32);
            let actions: BTreeSet<_> = b
                .hits
                .values()
                .filter_map(|h| {
                    if let Hit::Action(i) = h {
                        Some(i)
                    } else {
                        None
                    }
                })
                .collect();
            assert_eq!(actions.len(), 6);
            if w >= 100 {
                assert_eq!(
                    b.hits.keys().map(|(_, y)| y).collect::<BTreeSet<_>>().len(),
                    1
                );
            }
        }
    }
    #[test]
    fn inactive_unknown_does_not_pollute_label() {
        let mut r = rows().remove(0);
        r["routes"]
            .as_array_mut()
            .unwrap()
            .push(json!({"enabled":false,"permission":{"label":"Access unknown"}}));
        assert_eq!(permission(&r), "Project Access");
        r["routes"][1]["enabled"] = json!(true);
        assert_eq!(permission(&r), "Check permissions");
    }
    #[test]
    fn waiting_conversations_are_visible_and_paused_routes_do_not_alarm() {
        for (state, label) in [
            ("waiting-for-approval", "Approval needed"),
            ("waiting-for-user-input", "Response needed"),
        ] {
            let mut data = rows();
            data[0]["routes"][0]["owner_health"] = json!({"status":state,"ready":false,"attention":{"status":"required"},"reason":"Open in Codex to review and respond."});
            assert_eq!(ready(&data[0]), label);
            let text = build(&json!({}), &data, 0, 0, true, false, 0, 0, "", 100, 32).render();
            assert!(text.contains(label));
            assert!(text.contains("Open in Codex"));
            for height in [24, 32] {
                let overview =
                    build(&json!({}), &data, 0, 0, false, false, 0, 0, "", 100, height).render();
                assert!(overview.contains("NEEDS ATTENTION"));
                assert!(overview.contains("1 waiting"));
                assert!(overview.contains(label));
            }
            data[0]["routes"][0]["enabled"] = json!(false);
            assert_eq!(ready(&data[0]), "Paused");
        }
    }

    #[test]
    fn server_scope_is_explicit() {
        let b = build(&json!({}), &rows(), 0, 0, true, true, 0, 0, "", 100, 32);
        assert!(b.render().contains("ALL conversations"));
    }
}
