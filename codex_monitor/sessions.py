"""On-demand session overview; never contacts Codex or creates a model turn."""
from .lock import process_alive


def _assessment(sessions, receiver_running):
    """Describe configuration and process state without inferring producer health."""
    if not sessions:
        return {
            "delivery_enabled": False,
            "assessment": "no sessions attached",
            "action": "attach a session before starting the receiver",
        }

    enabled = [session["enabled"] for session in sessions]
    if all(enabled):
        delivery = "enabled"
    elif any(enabled):
        delivery = "partly paused"
    else:
        delivery = "paused"

    actions = []
    if not all(enabled):
        actions.append("To resume: unpause the paused binding(s)")
    if not receiver_running:
        actions.append("start the receiver with `codex-monitor serve`")
    actions.append("check the external producer separately; its health is unverified")
    return {
        "delivery_enabled": any(enabled),
        "assessment": f"delivery {delivery}; receiver {'running' if receiver_running else 'stopped'}; source unverified",
        "action": "; ".join(actions),
    }


def overview(monitor, name=None):
    bindings = monitor.bindings()
    if name is not None:
        bindings = [binding for binding in bindings if binding["name"] == name]
        if not bindings:
            raise ValueError("unknown session binding")
    sessions = []
    with monitor.connect() as db:
        for binding in bindings:
            counts = {row["state"]: row["n"] for row in db.execute(
                "SELECT state,count(*) n FROM events WHERE binding=? GROUP BY state", (binding["name"],))}
            row = db.execute("SELECT id,type FROM (SELECT id,json_extract(envelope,'$.type') type,seq FROM events WHERE binding=?) ORDER BY seq DESC LIMIT 1",
                             (binding["name"],)).fetchone()
            sessions.append({**binding, "enabled": bool(binding["enabled"]), "events": counts,
                             "producer_health": "unknown", "target_verification": "not_checked",
                             "last_event": dict(row) if row else None})
    receiver_running = process_alive(monitor.root / "serve.lock")
    return {"receiver_running": receiver_running, "sessions": sessions,
            **_assessment(sessions, receiver_running),
            "note": ("Receiver process and binding configuration only; source health and model activity are not inferred. "
                     "An accepted event means Codex storage accepted it; it does not prove model completion or task success. "
                     "Use `codex-monitor inspect DELIVERY_ID` to compare local acceptance with native queue/history evidence.")}


def display(value):
    receiver = "running (process is alive)" if value["receiver_running"] else "stopped (process is not alive)"
    lines = [f"Assessment: {value['assessment']}", f"Action: {value['action']}", f"Receiver: {receiver}"]
    for session in value["sessions"]:
        state = "enabled (delivery allowed)" if session["enabled"] else "paused (delivery blocked)"
        lines += [f"\n{session['name']} — binding {state}", f"  Conversation: {session['thread']}",
                  "  Sources: " + ", ".join(session["sources"]) + " (external producer health unverified)",
                  "  Target: not checked (use doctor --thread with this conversation ID)",
                  "  Events: " + (", ".join(f"{k}={v}" for k, v in session["events"].items()) or "none")]
        if session["last_event"]:
            delivery_id = session["last_event"]["id"]
            lines.append("  Latest receipt: " + session["last_event"]["type"] + " (" + delivery_id + ")")
            lines.append("  Inspect: codex-monitor inspect " + delivery_id)
    if not value["sessions"]:
        lines.append("No sessions attached. Use attach NAME --thread THREAD --source SOURCE.")
    lines.append("\n" + value["note"])
    return "\n".join(lines)
