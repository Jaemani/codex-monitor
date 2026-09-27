"""Terminal-native dashboard layout and shared pointer/navigation geometry.

This module only presents inventory data. It never probes services or performs
monitoring actions; the controller owns those operations.
"""

from dataclasses import dataclass, field


@dataclass
class Board:
    width: int
    height: int
    color: bool
    hits: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    detail_offset: int = 0
    detail_limit: int = 0

    def __post_init__(self):
        self.cells = [[(" ", "") for _ in range(self.width)] for _ in range(self.height)]

    def text(self, x, y, text, style="", limit=None):
        from .dashboard import _safe, _cell_width
        if not 0 <= y < self.height:
            return
        end = min(self.width, x + (limit if limit is not None else self.width))
        for char in _safe(text, 10000):
            size = _cell_width(char)
            if x + size > end:
                break
            if size == 0:
                if 0 < x <= self.width:
                    old, attr = self.cells[y][x - 1]
                    self.cells[y][x - 1] = (old + char, attr)
                continue
            if x >= 0:
                self.cells[y][x] = (char, style)
                if size == 2 and x + 1 < self.width:
                    self.cells[y][x + 1] = ("", style)
            x += size

    def box(self, x, y, w, h, title, style="38;5;240"):
        if w < 2 or h < 2:
            return
        self.text(x, y, "╭" + "─" * (w - 2) + "╮", style, w)
        for row in range(y + 1, y + h - 1):
            self.text(x, row, "│" + " " * (w - 2) + "│", style, w)
        self.text(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", style, w)
        self.text(x + 2, y, " " + title + " ", "1;36" if style == "36" else "1;38;5;250", w - 4)

    def hit(self, x, y, w, target):
        for col in range(max(0, x), min(self.width, x + w)):
            if 0 <= y < self.height:
                self.hits[(col, y)] = target

    def render(self):
        lines = []
        for row in self.cells:
            line, previous = "", ""
            for char, style in row:
                if self.color and style != previous:
                    line += "\x1b[0m" + (f"\x1b[{style}m" if style else "")
                    previous = style
                line += char
            lines.append(line.rstrip() + ("\x1b[0m" if self.color else ""))
        return "\n".join(lines)


def project_layout(connections, width, stride=1):
    """Pack whole project panels into the shortest column, retaining row IDs."""
    groups = {}
    for index, connection in enumerate(connections):
        groups.setdefault(connection.get("project") or "Ungrouped", []).append(index)
    columns = 3 if width >= 150 else 2 if width >= 76 else 1
    gap = 2
    panel_width = (width - gap * (columns - 1)) // columns
    bottoms = [0] * columns
    panels, positions = [], {}
    for project, indices in sorted(groups.items(), key=lambda item: -len(item[1])):
        column = min(range(columns), key=lambda i: bottoms[i])
        x, y = column * (panel_width + gap), bottoms[column]
        h = len(indices) * stride + 3
        panels.append((x, y, panel_width, h, project, indices))
        for offset, index in enumerate(indices):
            positions[index] = (x, y + 2 + offset * stride)
        bottoms[column] += h + 1
    return panels, positions, max(bottoms, default=0)


def navigate(connections, width, selected, direction, height=32):
    """Move spatially, rather than jumping through an invisible list order."""
    _, positions, _ = project_layout(connections, width, 2 if height >= 40 or width < 50 else 1)
    if selected not in positions:
        return 0
    x, y = positions[selected]
    candidates = []
    for index, (px, py) in positions.items():
        dx, dy = px - x, py - y
        if direction in ("up", "down"):
            if dx == 0 and dy * (1 if direction == "down" else -1) > 0:
                candidates.append((abs(dy), index))
        elif dx * (1 if direction == "right" else -1) > 0:
            candidates.append((abs(dx) + abs(dy), index))
    return min(candidates)[1] if candidates else selected


def retain_selection(previous, current, selected, route):
    """Resolve the displayed identity again after inventory refresh."""
    from .dashboard import _preferred_route
    old = previous.get("connections") or []
    new = current.get("connections") or []
    if not old or not new or selected >= len(old):
        return 0, _preferred_route(new[0]) if new else 0, False
    before = old[selected]
    bindings = before.get("bindings") or []
    binding = bindings[route % len(bindings)] if bindings else {}
    for index, connection in enumerate(new):
        if connection.get("thread") == before.get("thread"):
            for next_route, candidate in enumerate(connection.get("bindings") or []):
                if (candidate.get("name"), candidate.get("endpoint")) == (binding.get("name"), binding.get("endpoint")):
                    return index, next_route, True
            return index, _preferred_route(connection), False
    return 0, _preferred_route(new[0]), False


def wrap_cells(text, width):
    """Wrap diagnostics without dropping double-width or combining characters."""
    from .dashboard import _cell_width
    result, line, used = [], "", 0
    for char in text:
        size = _cell_width(char)
        if used + size > width and line:
            result.append(line)
            line, used = "", 0
        line += char
        used += size
    return result + [line]


def _bytes(value):
    if value is None:
        return "Unknown"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{sign}{value:.1f} {unit}"
        value /= 1024


def project_resource_line(snapshot, project):
    value = ((snapshot.get("resources") or {}).get("projects") or {}).get(project) or {}
    if not value.get("available"):
        return "Monitor resources: Unknown"
    if not value["workers"]:
        return "Monitor: no active samplers"
    return f"Monitor: {value['workers']} workers · {_bytes(value['rss_bytes'])} · CPU {value['cpu_percent']}%"


def build(snapshot, *, width=100, height=32, color=False, selected=0,
          selected_route=0, detail=False, detail_scroll=0, notice=None,
          notice_kind="OPEN", action=0, now=None, permission_menu=False, **unused):
    from . import dashboard as d
    width, height = max(1, int(width)), max(2, int(height))
    board = Board(width, height, color)
    connections = snapshot.get("connections") or []
    selected = min(max(0, selected), max(0, len(connections) - 1))
    states = [d._conversation_status(c) for c in connections]
    issues = [i for i, state in enumerate(states) if state in {"ERROR", "STALE"}]
    unknown = states.count("UNKNOWN")
    failures = sum(d._connection_state_label(c) in {"Login required", "Execution error"} for c in connections)
    pending = sum(int((b.get("events") or {}).get("counts", {}).get("pending", 0) or 0)
                  for c in connections for b in c.get("bindings") or [] if b.get("enabled"))
    ready = (snapshot.get("receiver") or {}).get("ready")
    board.text(1, 0, "CODEX MONITOR", "1;97")
    summary = f"{len(connections)} conversations · {len(issues)} need attention"
    board.text(max(18, width - len(summary) - 1), 0, summary, "38;5;250")
    if width >= 76 and height >= 20:
        metrics = [("RECEIVER", "Online" if ready else "Unavailable", "36" if ready else "33"),
                   ("EXECUTION", f"{failures} failed", "91" if failures else "37"),
                   ("DELIVERY", f"{pending} pending", "33" if pending else "37"),
                   ("VERIFICATION", f"{unknown} unchecked", "33" if unknown else "37")]
        step = width // 4
        for index, (label, value, style) in enumerate(metrics):
            x, w = index * step, step - 1 if index < 3 else width - index * step
            board.box(x, 2, w, 3, label)
            board.text(x + 2, 3, value, "1;" + style, w - 4)
        top = 6
    else:
        board.text(1, 1, f"Receiver {'online' if ready else 'unavailable'} · {failures} failed · {pending} pending · {unknown} unchecked", "33")
        top = 3
    if snapshot.get("ok") and height >= 32:
        ages = [b.get("events", {}).get("oldest_unresolved_age_seconds")
                for c in connections for b in c.get("bindings") or [] if b.get("enabled")]
        ages = [age for age in ages if isinstance(age, (int, float))]
        board.text(1, top, "Oldest unresolved  " + (d._age_text(max(ages)) if ages else "None recorded") + " · queue acceptance is not a completed reply", "33", width - 2)
        top += 2
    resources = snapshot.get("resources") or {}
    process = resources.get("process") or {}
    sampler = ((snapshot.get("receiver") or {}).get("runtime") or {}).get("sampler") or {}
    warning = ""
    if process.get("consecutive_zombie_samples", 0) >= 2:
        warning = f"RESOURCE ALERT: zombies in {process['consecutive_zombie_samples']} consecutive samples; inspect reaping"
    if sampler.get("spawning_suspended"):
        warning += " · Sampler spawning suspended"
    if warning:
        board.text(1, top, warning, "1;91", width - 2)
        top += 1
    if not snapshot.get("ok"):
        board.text(1, top, "Inventory unavailable", "1;91")
        board.text(1, top + 1, d._safe(snapshot.get("error")), "37")
        board.text(1, height - 1, "Retrying automatically · Esc quit", "38;5;250")
        return board
    elif issues and height >= 25:
        cols = 2 if width >= 90 else 1
        slots = min(len(issues), 4)
        rows = (slots + cols - 1) // cols
        board.box(0, top, width, rows * 2 + 2, "NEEDS ATTENTION", "33")
        for slot, index in enumerate(issues[:slots]):
            c = connections[index]
            x, y = 2 + (slot % cols) * (width // cols), top + 1 + (slot // cols) * 2
            limit = width // cols - 4
            board.text(x, y, f"{c.get('project') or 'Ungrouped'} / {d._conversation_label(c)}", "1;97", limit)
            board.text(x, y + 1, d._connection_next_step(c), "33", limit)
            board.hit(x, y, limit, ("conversation", index))
            board.hit(x, y + 1, limit, ("conversation", index))
        top += rows * 2 + 3
    resource_height = 0
    available = max(1, height - top - 2 - resource_height)
    stride = 2 if height >= 40 or width < 50 or any(c.get("permission_label") not in (None, "Unknown") for c in connections) else 1
    panels, positions, total = project_layout(connections, width, stride)
    selected_y = positions.get(selected, (0, 0))[1]
    offset = max(0, selected_y - available + 2)
    # Render in a separate surface so scrolled cards cannot overwrite summaries.
    body = Board(width, available, color)
    for x, y, w, h, project, indices in panels:
        y -= offset
        body.box(x, y, w, h, f"{project} · {len(indices)} " + ("conversation" if len(indices) == 1 else "conversations"), "36" if selected in indices else "38;5;240")
        if resources:
            body.text(x + 2, y + 1, project_resource_line(snapshot, project), "38;5;250", w - 4)
        for row, index in enumerate(indices):
            c = connections[index]
            state = states[index]
            label = {"ON": "Ready", "OFF": "Paused", "UNKNOWN": "Unchecked"}.get(state, d._connection_state_label(c))
            style = {"ON": "36", "OFF": "38;5;245", "UNKNOWN": "33", "STALE": "33"}.get(state, "91")
            py = y + 2 + row * stride
            if index == selected:
                body.text(x + 1, py, " " * (w - 2), "48;5;237")
            prefix = "› " if index == selected else "  "
            label_width = min(17, max(8, w // 2 - 3))
            body.text(x + 2, py, prefix + d._conversation_label(c), "1;97" + (";48;5;237" if index == selected else ""), w - label_width - 4)
            body.text(x + w - label_width - 2, py, label, style + (";48;5;237" if index == selected else ""), label_width)
            if stride == 2:
                body.text(x + 4, py + 1, _permission_name(c.get("permission_label")) + " · " + d._connection_last_seen(c), "38;5;250", w - 6)
                body.hit(x + 1, py + 1, w - 2, ("conversation", index))
            body.hit(x + 1, py, w - 2, ("conversation", index))
            if 0 <= py < available:
                board.positions[index] = (x, top + py)
        if selected in indices and y < 0 and available > 2:
            body.text(x, 0, "─" * w, "36", w)
            body.text(x + 2, 0, f" {project} · continued ", "1;36", w - 4)
            body.hits = {point: target for point, target in body.hits.items()
                         if point[1] != 0 or not x <= point[0] < x + w}
    for row in range(available):
        if top + row < height - 1:
            board.cells[top + row] = body.cells[row]
    board.hits.update({(x, y + top): target for (x, y), target in body.hits.items()})
    if not connections and snapshot.get("ok"):
        board.text(2, top + 1, "No conversations registered", "37")
    footer = "↑↓←→ select   Enter details   Click to inspect   Esc quit"
    if width < 65:
        footer = "↑↓ · Enter details · Esc quit"
    if width < 30:
        footer = "↑↓ Enter details Esc quit"
    board.text(1, height - 1, footer, "38;5;250")
    if total > available:
        board.text(max(1, width - 18), height - 2, "↑↓ more projects", "38;5;250")
    if height >= 20 and not notice:
        freshness = d._refresh_line(snapshot, d.time.time() if now is None else now)
        board.text(1, height - 2, "Ready = delivery available · Reply completion is separate", "38;5;250", max(1, width - 22))
        if width > len(footer) + len(freshness) + 4:
            board.text(width - len(freshness) - 1, height - 1, freshness, "38;5;250")
    if notice:
        board.text(1, height - 2, f"{notice_kind}: {notice}", "33")
    if detail and connections:
        _overlay(board, snapshot, selected, selected_route, detail_scroll, action, notice, notice_kind, permission_menu)
    return board


def _permission_name(label):
    return {"Unknown": "Access unknown", "Mixed": "Check permissions", None: "Access unknown"}.get(label, label)


def _overlay(board, snapshot, selected, route, scroll, action, notice, notice_kind, permission_menu=False):
    from . import dashboard as d
    c = snapshot["connections"][selected]
    bindings = c.get("bindings") or []
    binding = bindings[route % len(bindings)] if bindings else {}
    w, h = min(112, board.width), min(24, board.height)
    x, y = (board.width - w) // 2, (board.height - h) // 2
    # Dim the preserved overview; all pointer targets now belong to the modal.
    board.cells = [[(char, "38;5;240") for char, _ in row] for row in board.cells]
    board.hits.clear()
    board.box(x, y, w, h, f"{c.get('project') or 'Ungrouped'} / {d._conversation_label(c)}", "36")
    lines = [d._connection_state_label(c), d._connection_next_step(c),
             "Saved permissions: " + _permission_name((binding.get("permission") or {}).get("label", c.get("permission_label"))), "",
             "CONNECTION", d._binding_label(c, binding) + (" · enabled" if binding.get("enabled") else " · paused"),
             f"{route % len(bindings) + 1 if bindings else 0}/{len(bindings)} connections · Tab for next", "",
             "DELIVERY", d._backlog_attention(c) or d._last_delivery(binding), "",
             "EXECUTION", d._work_report_summary(c.get("requests") or {}) if (c.get("requests") or {}).get("counts") else "Completion not reported"]
    permission_label = (binding.get("permission") or {}).get("label", c.get("permission_label"))
    if permission_label == "Project Access":
        lines[3:3] = ["Can edit project files and use the internet. Writes outside the project are restricted."]
    lines[4:4] = ["From saved service settings; current runtime permissions have not been verified."]
    owner = binding.get("owner_health") or {}
    execution_error = owner.get("execution_error") or {}
    issue = execution_error.get("message")
    if owner.get("status") == "execution-error" and not issue:
        issue = ("The owner does not support recent-turn error details. Open in Codex to inspect."
                 if execution_error.get("status") == "unsupported" else
                 "Recent-turn error details are unavailable. Open in Codex to inspect.")
    # Put the native failure before delivery history, where it is visible on
    # first opening the overlay. A prior delivery error must not hide it.
    if issue:
        lines[2:2] = ["", "EXECUTION ERROR", d._safe(issue, 2000),
                      "Reconnect reloads the current login. Open in Codex to continue failed work."
                      if execution_error.get("kind") == "authentication" else "Open in Codex to inspect the failed run."]
    if notice:
        lines[0:0] = [notice_kind, d._safe(notice, 2000), ""]
    error = ((binding.get("events") or {}).get("latest") or {}).get("error") or binding.get("schema_error")
    if not error and owner.get("status") in {"auth-required", "execution-error", "unavailable", "unloaded"}:
        error = owner.get("reason")
    if not error:
        error = next((item.get("last_error") or item.get("last_sample_error")
                      for item in c.get("collectors") or []
                      if item.get("binding") == binding.get("name")
                      and (item.get("last_error") or item.get("last_sample_error"))), None)
    receiver_reason = (snapshot.get("receiver") or {}).get("reason")
    if receiver_reason:
        lines.extend(["", "RECEIVER", d._safe(receiver_reason, 500)])
    if error:
        lines.extend(["", "REPORTED ISSUE", d._safe(error, 500)])
    lines.extend(["", "RECONNECT CURRENT LOGIN",
                  "Reconnect previews a shared owner restart. Select it again to confirm.",
                  "Active turns block reconnect. Existing windows disconnect; residents restore the same conversations.",
                  "Change Permission opens a separate menu; Reconnect retains the current permissions.",
                  "Permission changes require empty queues and a second click on the SAME action. Defaults persist until changed.",
                  "Full permits external setup writes. Read-only blocks writes and command networking. Project Access permits project writes and outbound networking.",
                  "Discord thread creation requires permission preflight and user choice for the required API and local setup operations; full access itself is not mandatory."])
    lines.extend(["", "PROJECT RESOURCES",
                  project_resource_line(snapshot, c.get("project") or "Ungrouped"),
                  "Monitor samplers only. Agent CPU/RAM and shared DB/log storage are not attributed."])
    metrics = (binding.get("events") or {}).get("delivery_metrics") or {}
    age = (binding.get("events") or {}).get("oldest_unresolved_age_seconds")
    counts = (binding.get("events") or {}).get("counts") or {}
    lines.extend(["", "DELIVERY HEALTH",
                  "Oldest unresolved: " + (d._age_text(age) if age is not None else "None recorded"),
                  f"Failed: {counts.get('dead', 0)} · Uncertain: {counts.get('uncertain', 0)} · Retrying: {metrics.get('retrying_unresolved', 'Unknown')}"])
    latency = metrics.get("acceptance_latency_median_seconds")
    lines.append("Queue acceptance median: " + (d._age_text(latency) + f" (last {metrics['accepted_sample_size']})" if latency is not None else "Unknown"))
    lines.append("Reply completion latency: Unknown (not measured)")
    for collector in c.get("collectors") or []:
        if collector.get("binding") == binding.get("name") and collector.get("path"):
            lines.extend(["", "SOURCE", d._safe(collector["path"], 1000)])
    if permission_menu:
        lines = ["CHANGE PERMISSION", "", "Current saved policy and change preview:", d._safe(notice or "Reading saved owner permissions...", 5000), "",
                 "Full access: unrestricted filesystem and command network.",
                 "Read-only: no writes or command network; Discord reply commands may fail.",
                 "Project Access: edit project files and use the internet. Writes to other folders are restricted.", "",
                 "Applies to ALL conversations sharing this owner. Settings persist until changed.",
                 "Select a mode to preview the affected IDs; select the SAME mode again to apply and reconnect.",
                 "Back or Esc returns without applying a new choice."]
    labels = (["Full Access", "Read-only", "Project Access", "Back"] if permission_menu else
              ["Open in Codex", "Pause" if binding.get("enabled") else "Resume", "Remove", "Reconnect", "Change Permission", "Close"])
    button_rows, row, used = [], [], 0
    for index, label in enumerate(labels):
        size = min(len(label) + 4, max(1, w - 4))
        if row and used + 1 + size > w - 4:
            button_rows.append(row)
            row, used = [], 0
        row.append((index, label, size))
        used += size + (1 if len(row) > 1 else 0)
    if row:
        button_rows.append(row)
    button_top = y + h - 2 - len(button_rows)
    content_width = max(1, w - 6)
    wrapped = [part for line in lines for part in wrap_cells(line, content_width)]
    slots = max(1, button_top - (y + 2) - 2)
    offset = min(max(0, scroll), max(0, len(wrapped) - slots))
    board.detail_offset, board.detail_limit = offset, max(0, len(wrapped) - slots)
    for row, line in enumerate(wrapped[offset:offset + slots]):
        board.text(x + 3, y + 2 + row, line, "1;97" if line.isupper() else "37", content_width)
    if len(wrapped) > slots:
        board.text(x + 3, button_top - 1, f"↑↓ scroll  {offset + 1}–{min(offset + slots, len(wrapped))}/{len(wrapped)}", "38;5;250", content_width)
    for row_index, buttons in enumerate(button_rows):
        cursor = x + 2
        for index, label, size in buttons:
            text = f"[ {label} ]"
            board.text(cursor, button_top + row_index, text, "1;30;46" if action == index else "37", size)
            board.hit(cursor, button_top + row_index, size, ("action", index))
            cursor += size + 1
    board.text(x + 2, y + h - 2, f"←→ action {action + 1}/{len(labels)} · Enter · Esc close", "38;5;250", w - 4)
