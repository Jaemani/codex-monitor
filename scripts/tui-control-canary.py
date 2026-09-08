#!/usr/bin/env python3
"""Real Codex TUI approval-wait and user-interrupt acceptance canary."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import time

import pyte

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_monitor.cli import send
from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.session import Rpc, SessionPool


class Terminal:
    def __init__(self, argv):
        self.screen = pyte.HistoryScreen(120, 40, history=1000)
        self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("NO_COLOR", None)
            os.execvp(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    def pump(self, duration=.1):
        if select.select([self.fd], [], [], duration)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            self.stream.feed(data)
            if b"\x1b[6n" in data:
                os.write(self.fd, b"\x1b[1;1R")
            if b"\x1b[c" in data:
                os.write(self.fd, b"\x1b[?1;2c")

    def text(self):
        return "\n".join(self.screen.display)

    def input(self, value):
        os.write(self.fd, value.encode())

    def close(self):
        try:
            os.killpg(self.pid, signal.SIGTERM)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    break
                self.pump()
            else:
                os.killpg(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    terminal_path = args.report.with_suffix(".terminal.txt")
    started = time.monotonic()
    report = {
        "result": "RUNNING",
        "surface": "cli",
        "ui_kind": "real TUI in owned disposable PTY",
        "evidence_kind": "pyte-rendered terminal buffer captured from the owned PTY; not a GUI screenshot",
        "terminal_buffer_capture": str(terminal_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "steps": [],
        "process_model": {
            "harness_pid": os.getpid(),
            "monitor": "in-process HTTP Server and dispatch thread; no separate monitor process",
        },
    }
    terminal = server = rpc = None
    pool = SessionPool()
    thread = None
    buffers = {}

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(args.report)

    def step(name, **details):
        value = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        report["steps"].append(value)
        save()
        print(json.dumps({"progress": value}), flush=True)

    def capture(name):
        terminal.pump(0)
        buffers[name] = terminal.text()
        temporary = terminal_path.with_suffix(terminal_path.suffix + ".tmp")
        temporary.write_text("\n\n".join(f"=== {key} ===\n{value}" for key, value in buffers.items()))
        temporary.replace(terminal_path)

    save()
    try:
        with tempfile.TemporaryDirectory(prefix="cm-tui-control-", dir="/tmp") as tmp:
            work = Path(tmp).resolve() / "work"
            work.mkdir()
            marker_file = work / "approval_marker"
            prompt = (
                "This is an isolated terminal acceptance test in a disposable directory. "
                "Use the shell to run exactly `touch approval_marker`; do not simulate it and do not run another command. "
                "Wait for user approval if required. For any later user or external message containing a marker, "
                "reply exactly ACK: followed by its marker, with no spaces."
            )
            argv = [
                "codex", "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "on-request",
                "-c", "model_reasoning_effort=\"low\"", "-c", "tui.animations=false", prompt,
            ]
            terminal = Terminal(argv)
            report["process_model"]["tui_pid"] = terminal.pid
            step("tui_started", tui_pid=terminal.pid, cwd=str(work))
            rpc = Rpc("shared-local")
            last_trust_input = 0.0

            def wait_for(check, timeout=90, label="condition"):
                nonlocal last_trust_input
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    terminal.pump()
                    screen = terminal.text()
                    now = time.monotonic()
                    if "trust" in screen.lower() and "Yes, continue" in screen and now - last_trust_input >= .5:
                        terminal.input("\r")
                        last_trust_input = now
                    if check():
                        return
                raise TimeoutError(f"{label} timed out; see terminal buffer artifact")

            def turns():
                return rpc.call(
                    "thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 100}
                )["data"]

            def approval_visible():
                screen = terminal.text().lower()
                return (
                    "would you like to run the following command" in screen
                    or "yes, proceed" in screen
                    or "allow command" in screen
                )

            def agent_ack_count(marker):
                expected = "ACK:" + marker
                return sum(
                    item.get("type") == "agentMessage" and item.get("text", "").strip() == expected
                    for turn in turns() for item in turn.get("items", [])
                )

            def screen_has_ack(marker):
                expected = "ACK:" + marker
                return any(
                    line.strip() in ("• " + expected, "● " + expected)
                    for line in terminal.text().splitlines()
                )

            def submit_user(text):
                terminal.input(text)
                for _ in range(5):
                    terminal.pump()
                terminal.input("\r")

            wait_for(approval_visible, label="approval prompt")
            rows = rpc.call("thread/list", {"cwd": str(work), "sourceKinds": ["cli"], "limit": 10})["data"]
            if len(rows) != 1:
                raise AssertionError(f"expected exactly one owned CLI thread, found {len(rows)}")
            thread = rows[0]["id"]
            report["thread"] = thread
            if marker_file.exists():
                raise AssertionError("sandboxed command ran before approval")
            capture("approval_wait")
            step("approval_prompt_visible", thread=thread)

            monitor = Monitor(Path(tmp) / "state", pool)
            monitor.bind("control", thread, "shared-local", ["test"])
            server = Server(monitor, {"test": "isolated-tui-control"}, "isolated-tui-admin", port=0).start()
            envelope = {
                "id": "TUI_APPROVAL_EVENT",
                "source": "test",
                "type": "test.event",
                "data": {"marker": "TUI_APPROVAL_EVENT"},
            }
            receipt = send(server.url, "control", envelope, "isolated-tui-control")
            duplicate = send(server.url, "control", envelope, "isolated-tui-control")
            if receipt["delivery_id"] != duplicate["delivery_id"]:
                raise AssertionError("duplicate receipt changed")
            deadline = time.monotonic() + 10
            while monitor.event(receipt["delivery_id"])["state"] != "accepted" and time.monotonic() < deadline:
                terminal.pump(.1)
            if monitor.event(receipt["delivery_id"])["state"] != "accepted":
                raise AssertionError("event was not durably queued during approval wait")
            observe_until = time.monotonic() + 3
            while time.monotonic() < observe_until:
                terminal.pump(.1)
            if marker_file.exists() or not approval_visible() or agent_ack_count("TUI_APPROVAL_EVENT"):
                raise AssertionError("queued event approved the command or bypassed the approval wait")
            capture("event_queued_approval_still_waiting")
            report["checks"]["event_does_not_auto_approve_waiting_command"] = True
            report["checks"]["sandboxed_file_absent_without_approval"] = True
            step("event_queued_without_auto_approval", delivery_id=receipt["delivery_id"])

            terminal.input("\x03")

            def interrupted_turn():
                return any(turn.get("status") == "interrupted" for turn in turns())

            wait_for(interrupted_turn, 30, "native interrupted turn after Ctrl+C")
            step("user_interrupt_recorded", input="Ctrl+C")
            observe_until = time.monotonic() + 3
            while time.monotonic() < observe_until:
                terminal.pump(.1)
            client = monitor.event(receipt["delivery_id"])["client_id"]
            queued = rpc.call("thread/queue/list", {"threadId": thread, "limit": 100})["data"]
            event_waited = any(item.get("clientUserMessageId") == client for item in queued)
            if marker_file.exists():
                raise AssertionError("user interrupt unexpectedly created the marker file")
            if event_waited and agent_ack_count("TUI_APPROVAL_EVENT"):
                raise AssertionError("queued event was both pending and present in agent history")
            report["checks"]["user_cancellation_remains_respected_while_event_waits"] = True
            step("post_interrupt_state_observed", event_still_queued=event_waited)

            submit_user("marker TUI_CONTROL_AFTER")
            pending_screens = {"TUI_APPROVAL_EVENT", "TUI_CONTROL_AFTER"}
            deadline = time.monotonic() + 90
            while pending_screens and time.monotonic() < deadline:
                terminal.pump(.1)
                for marker in tuple(pending_screens):
                    if screen_has_ack(marker):
                        capture("visible_" + marker.lower())
                        pending_screens.remove(marker)
            if pending_screens:
                raise TimeoutError(f"missing rendered ACKs after user follow-up: {sorted(pending_screens)}")
            wait_for(
                lambda: agent_ack_count("TUI_APPROVAL_EVENT") == 1
                and agent_ack_count("TUI_CONTROL_AFTER") == 1
                and all(turn.get("status") in ("completed", "interrupted") for turn in turns()),
                90,
                "completed queued event and user follow-up after interrupt",
            )
            if marker_file.exists():
                raise AssertionError("interrupted approval unexpectedly created the marker file")
            items = [item for turn in turns() for item in turn.get("items", [])]
            if sum(item.get("clientId") == client for item in items) != 1:
                raise AssertionError("queued event native history is not exactly once")
            if any(item.get("clientUserMessageId") == client for item in
                   rpc.call("thread/queue/list", {"threadId": thread, "limit": 100})["data"]):
                raise AssertionError("queued event remained pending after user follow-up")
            if pool("shared-local").rpc.call("thread/loaded/list", {})["data"]:
                raise AssertionError("writer loaded a thread")
            capture("queued_event_completed_after_interrupt")
            report["checks"].update(
                ctrl_c_interrupts_waiting_turn=True,
                queued_event_recovers_once_after_user_followup=True,
                user_followup_completes_once=True,
                duplicate_history_once=True,
                writer_loaded_no_threads=True,
            )
            report["result"] = "PASS"
            step("acceptance_completed", result="PASS")
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        step("acceptance_failed", error=report["error"])
    finally:
        if terminal:
            capture("last")
            terminal.close()
        if server:
            server.close()
        pool.close()
        if rpc:
            if thread:
                try:
                    rpc.call("thread/archive", {"threadId": thread})
                except Exception:
                    pass
            rpc.close()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
