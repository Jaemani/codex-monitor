"""On-demand session overview; never contacts Codex or creates a model turn."""
from .lock import process_alive


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
    return {"receiver_running": process_alive(monitor.root / "serve.lock"), "sessions": sessions,
            "note": "Receiver process and binding configuration only; source health and model activity are not inferred."}


def display(value):
    lines = ["Receiver: " + ("running" if value["receiver_running"] else "stopped")]
    for session in value["sessions"]:
        state = "enabled" if session["enabled"] else "paused"
        lines += [f"\n{session['name']} — binding {state}", f"  Conversation: {session['thread']}",
                  "  Sources: " + ", ".join(session["sources"]),
                  "  Producer: unknown (not supervised by receiver)",
                  "  Target: not checked (use doctor --thread with this conversation ID)",
                  "  Events: " + (", ".join(f"{k}={v}" for k, v in session["events"].items()) or "none")]
        if session["last_event"]:
            lines.append("  Latest: " + session["last_event"]["type"] + " (" + session["last_event"]["id"] + ")")
    if not value["sessions"]:
        lines.append("No sessions attached. Use attach NAME --thread THREAD --source SOURCE.")
    lines.append("\nUse inspect DELIVERY_ID for native delivery evidence. Source health is not inferred.")
    return "\n".join(lines)
