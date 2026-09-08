#!/usr/bin/env python3
"""Opt-in same-Desktop event and explicit reply round trip to an owned source."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.cli import main as cli
from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.session import SessionPool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enqueue", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--thread")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.enqueue and (not args.thread or args.report.exists()):
        parser.error("enqueue needs an authorized thread and a new report")
    state = args.report.resolve().parent / ".runtime" / args.report.stem
    def run(*command):
        out = io.StringIO()
        with redirect_stdout(out):
            result = cli(["--state", str(state), *command])
        if result:
            raise RuntimeError(f"CLI {command[0]} failed")
        return json.loads(out.getvalue())
    pool = SessionPool()
    server = None
    report = {"result": "FAIL", "thread": args.thread}
    try:
        if args.enqueue:
            run("init")
            run("source", "verification")
            run("attach", "session-ux", "--thread", args.thread, "--source", "verification")
        else:
            report = json.loads(args.report.read_text())
        config = json.loads((state / "config.json").read_text())
        token = Path(config["sources"]["verification"]["token_file"]).read_text().strip()
        monitor = Monitor(state, pool)
        server = Server(monitor, {"verification": token}, (state / "admin.token").read_text().strip(), port=0).start(dispatch=args.enqueue)
        def request(path, post=False, payload=None):
            body = json.dumps(payload).encode() if payload is not None else b"" if post else None
            req = urllib.request.Request(server.url + path, data=body,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        if args.enqueue:
            payload = {"id": args.report.stem, "source": "verification", "type": "monitor.test",
                "data": {"message": "This event verifies monitoring in the same conversation. Event details are readable, and an explicit reply can be sent to the original source."}}
            receipt = request("/v1/events/session-ux", payload=payload)
            deadline = time.monotonic() + 30
            while monitor.event(receipt["delivery_id"])["state"] != "accepted":
                if time.monotonic() >= deadline:
                    raise TimeoutError("queue acceptance timeout")
                time.sleep(.1)
            report.update(result="AWAITING_NATIVE_CONSUMPTION", delivery_id=receipt["delivery_id"],
                          sessions=run("sessions", "--json"))
        else:
            event = monitor.event(report["delivery_id"])
            native = pool("shared-local").inspect(report["thread"], event["client_id"])
            if native["state"] != "consumed":
                raise RuntimeError("event not yet in native history")
            receipt = run("reply", event["id"], "--id", "session-ux-confirmation", "--message",
                          "The event was received in the same conversation. The user can continue the conversation.")
            replies = request("/v1/replies")["data"]
            matching = [r for r in replies if r["id"] == receipt["reply_id"]]
            if not matching and not receipt["duplicate"]:
                raise AssertionError("source did not receive explicit reply")
            request("/v1/replies/" + receipt["reply_id"] + "/ack", post=True)
            if request("/v1/replies")["data"]:
                raise AssertionError("reply ack did not drain outbox")
            report.update(result="PASS", native=native, explicit_reply_retrieved_and_acknowledged=True,
                          local_event_unchanged=monitor.event(event["id"]) == event,
                          native_ui_pixels_verified=False)
    except Exception as exc:
        report.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
    finally:
        if server:
            server.close()
        pool.close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["result"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
