#!/usr/bin/env python3
"""Opt-in Rust receiver with an ordinary Codex TUI; raw reports stay outside Git."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from codex_monitor.session import Rpc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(ROOT):
        parser.error("raw report must be outside the repository")
    binary = ROOT / "rust/target/release/codex-monitor-rs"
    spec = importlib.util.spec_from_file_location("tui_helpers", ROOT / "scripts/tui-canary.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    report = {"result": "RUNNING", "checks": {}, "surface": "ordinary Codex remote TUI in owned PTY", "runtime": "Rust source release build", "version": subprocess.check_output(["codex", "--version"], text=True).strip()}
    started = time.monotonic()
    tmp = tempfile.TemporaryDirectory(prefix="cm-rust-tui-")
    work = Path(tmp.name)
    state = work / "state"
    terminal = owner = rpc = receiver = resident = None
    thread = None
    logs = []
    trusted = False

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")

    def cli(*argv):
        p = subprocess.run([str(binary), "--state", str(state), *argv], capture_output=True, text=True, timeout=30)
        if p.returncode:
            raise RuntimeError(p.stderr[:500])
        return json.loads(p.stdout)

    def pump():
        nonlocal trusted
        if terminal:
            terminal.pump(.05)
            screen = terminal.text().lower()
            if "would you like to run the following command" in screen or "approve this command" in screen:
                raise RuntimeError("unexpected approval; no approval sent")
            if not trusted and str(work).lower() in screen and ("yes, i trust" in screen or "yes, continue" in screen):
                terminal.input("\r")
                trusted = True

    def wait(predicate, label, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pump()
            if predicate():
                report["checks"][label] = True
                print(label, flush=True)
                save()
                return
            time.sleep(.1)
        raise TimeoutError(label)

    def turns():
        return rpc.call("thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 100})["data"]

    def agent_has(marker):
        return any(i.get("type") == "agentMessage" and i.get("text", "").strip() == marker for t in turns() for i in t.get("items", []))

    def start_receiver():
        log = (work / f"receiver-{len(logs)}.log").open("w")
        logs.append(log)
        p = subprocess.Popen([str(binary), "--state", str(state), "serve"], stdout=log, stderr=subprocess.STDOUT)
        try:
            wait(lambda: cli("status")["receiver"]["ready"], "receiver_ready", 15)
        except BaseException:
            stop(p)
            raise
        return p

    def stop(p):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def send_event(marker):
        receipt = cli("send", "--to", "canary", "--source", "test", "--id", marker, "--data", json.dumps({"message": marker}))
        delivery = cli("event", receipt["delivery_id"])
        client_id = delivery["client_id"]
        def count():
            return sum(i.get("type") == "userMessage" and i.get("clientId") == client_id for t in turns() for i in t.get("items", []))
        wait(lambda: count() == 1, marker + "_native_once")
        duplicate = cli("send", "--to", "canary", "--source", "test", "--id", marker, "--data", json.dumps({"message": marker}))
        assert duplicate["delivery_id"] == receipt["delivery_id"] and duplicate["duplicate"]
        report["checks"][marker + "_dedup"] = True
        return client_id

    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cli("init", "--port", str(port))
        cli("source", "test")
        owner = helper.RemoteServer(work / "owner.sock")
        rpc = Rpc(owner.endpoint)
        prompt = "This is an isolated monitor verification. Never use tools, read files or contact services. Reply exactly RUST_TUI_READY now. Later reply exactly RUST_TUI_FOLLOWUP to the user message VERIFY_FOLLOWUP. External events need no acknowledgement."
        base = ["codex", "--model", args.model, "--remote", owner.endpoint, "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never", "-c", 'model_reasoning_effort="low"', "-c", "tui.animations=false"]
        terminal = helper.Terminal(base + [prompt])
        def find():
            nonlocal thread
            rows = rpc.call("thread/list", {"cwd": str(work), "limit": 10})["data"]
            if len(rows) == 1:
                thread = rows[0]["id"]
            return bool(thread)
        wait(find, "tui_created_exact_thread")
        wait(lambda: agent_has("RUST_TUI_READY"), "initial_user_response")
        cli("bind", "canary", "--thread", thread, "--source", "test", "--endpoint", owner.endpoint)
        receiver = start_receiver()
        send_event("RUST_TUI_EVENT_A")
        wait(lambda: "RUST_TUI_EVENT_A" in terminal.text(), "event_visible_in_tui")
        terminal.input("VERIFY_FOLLOWUP")
        for _ in range(5):
            pump()
        terminal.input("\r")
        wait(lambda: agent_has("RUST_TUI_FOLLOWUP"), "user_followup_after_event")
        log = (work / "resident.log").open("w")
        logs.append(log)
        resident = subprocess.Popen([str(binary), "resident", "--endpoint", owner.endpoint, "--thread", thread], stdout=log, stderr=subprocess.STDOUT)
        wait(lambda: '"subscribed": true' in (work / "resident.log").read_text(), "resident_subscribed", 20)
        terminal.close()
        terminal = None
        send_event("RUST_TUI_CLOSED_EVENT")
        stop(receiver)
        receiver = start_receiver()
        send_event("RUST_TUI_RESTART_EVENT")
        terminal = helper.Terminal(base + ["resume", thread])
        wait(lambda: "RUST_TUI_RESTART_EVENT" in terminal.text(), "same_thread_reopen_history")
        terminal.close()
        terminal = None
        rpc.close()
        rpc = None
        owner.close()
        cli("send", "--to", "canary", "--source", "test", "--id", "RUST_OWNER_RESTART_EVENT", "--data", json.dumps({"message": "RUST_OWNER_RESTART_EVENT"}))
        owner = helper.RemoteServer(work / "owner.sock")
        rpc = Rpc(owner.endpoint)
        wait(lambda: thread in rpc.call("thread/loaded/list", {})["data"], "resident_reconnected_after_owner_restart", 30)
        send_event("RUST_OWNER_RESTART_EVENT")
        terminal = helper.Terminal(base + ["resume", thread])
        wait(lambda: "RUST_OWNER_RESTART_EVENT" in terminal.text(), "owner_restart_event_visible_in_same_tui")
        report["result"] = "PASS"
    except Exception as error:
        report["result"] = "FAIL"
        report["error"] = str(error)
    finally:
        if terminal:
            args.report.with_suffix(".terminal.txt").write_text(terminal.text())
            terminal.close()
        stop(resident)
        stop(receiver)
        if rpc and thread:
            try:
                rpc.call("thread/archive", {"threadId": thread})
                report["checks"]["owned_thread_archived"] = True
            except Exception as error:
                report["result"] = "FAIL"
                report["cleanup_error"] = str(error)
        if rpc:
            rpc.close()
        if owner:
            owner.close()
        for log in logs:
            log.close()
        tmp.cleanup()
        report["checks"]["temporary_state_removed"] = not work.exists()
        save()
    print(json.dumps(report), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
