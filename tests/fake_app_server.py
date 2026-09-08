"""A scripted App Server contract peer. Never invokes a model or a shell."""
import json
from pathlib import Path
import sys
import time

state_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
state = {
    "active": False, "queued": [], "history": [], "methods": [], "drop": False,
    "history_prefix": 0, "fail_next": None, "cycle_cursor": False, "error_after_add": False,
    "stop_reading": False,
}
if state_path and state_path.exists():
    state.update(json.loads(state_path.read_text()))


def save():
    if state_path:
        state_path.write_text(json.dumps(state))


def unavailable(thread):
    if thread == "archived-thread":
        return {"code": -32600, "message": f"session {thread} is archived"}
    if thread not in {"thread-user", "thread-other", "unloaded-desktop-thread"}:
        # Older stores use an internal code for this permanent lookup result.
        return {"code": -32603, "message": f"failed to read thread: no rollout found for thread id {thread}"}
    return None


def page(rows, params):
    cursor = params.get("cursor")
    offset = 0 if cursor == "cycle" else int(cursor or 0)
    limit = max(1, min(int(params.get("limit") or 100), 100))
    data = rows[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(rows) else None
    return {"data": data, "nextCursor": next_cursor}

for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg.get("method"), msg.get("params", {})
    if "id" not in msg:
        continue
    state["methods"].append(method)
    result = {}
    error = None
    fail = state["fail_next"]
    if fail and fail["method"] == method:
        error = {"code": fail["code"], "message": fail["message"]}
        state["fail_next"] = None
    elif method == "initialize":
        result = {"userAgent": "fake-codex/0.153.4"}
    elif method == "thread/loaded/list":
        result = {"data": ["thread-user", "thread-other"]}
    elif method == "thread/read":
        result = {"thread": {"id": params["threadId"], "status": {"type": "active" if state["active"] else "idle"}}}
    elif method == "thread/queue/add":
        error = unavailable(params["threadId"])
        entry = {"id": "queue-" + params["clientUserMessageId"], **params}
        if error:
            pass
        elif state["active"]:
            state["queued"].append(entry)
        else:
            state["history"].append({"threadId": params["threadId"], "type": "userMessage",
                                     "id": "message-" + params["clientUserMessageId"],
                                     "clientId": params["clientUserMessageId"], "content": params["input"]})
        if not error:
            result = {"queuedSubmission": entry}
        if not error and state["error_after_add"]:
            state["error_after_add"] = False
            error = {"code": -32603, "message": "failed to serialize queued submission"}
        if not error and state["drop"]:
            state["drop"] = False
            save()
            continue
    elif method == "thread/queue/list":
        error = unavailable(params["threadId"])
        if not error:
            rows = [row for row in state["queued"] if row["threadId"] == params["threadId"]]
            result = page(rows, params)
    elif method == "thread/turns/list":
        error = unavailable(params["threadId"])
        if not error:
            dummy = [{"id": f"turn-new-{i}", "items": []} for i in range(state["history_prefix"])]
            items = [{key: value for key, value in row.items() if key != "threadId"}
                     for row in state["history"] if row["threadId"] == params["threadId"]]
            result = page(dummy + [{"id": "turn-1", "items": items}], params)
            if state["cycle_cursor"]:
                result["nextCursor"] = "cycle"
    elif method == "test/active":
        state["active"] = params["value"]
        if not state["active"]:
            state["history"].extend({"threadId": row["threadId"], "type": "userMessage",
                                     "id": "message-" + row["clientUserMessageId"],
                                     "clientId": row["clientUserMessageId"], "content": row["input"]}
                                    for row in state["queued"])
            state["queued"] = []
    elif method == "test/drop-response":
        state["drop"] = True
    elif method == "test/history-prefix":
        state["history_prefix"] = params["count"]
    elif method == "test/fail-next":
        state["fail_next"] = params
    elif method == "test/cycle-cursor":
        state["cycle_cursor"] = True
    elif method == "test/error-after-add":
        state["error_after_add"] = True
    elif method == "test/stop-reading":
        state["stop_reading"] = True
    elif method == "test/exit":
        save()
        sys.exit(0)
    elif method == "test/methods":
        result = state["methods"]
    else:
        error = {"code": -32601, "message": "unsupported method: " + method}
    save()
    print(json.dumps({"id": msg["id"], **({"error": error} if error else {"result": result})}), flush=True)
    if state["stop_reading"]:
        while True:
            time.sleep(60)
