#!/usr/bin/env python3
"""Opt-in real Codex TUI in an owned PTY; requires test-only dependency pyte.

Controls only the CLI process this test creates, never an existing Terminal app.
"""
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
import subprocess
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
        self.raw = bytearray()

    def pump(self, duration=.1):
        if select.select([self.fd], [], [], duration)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            self.raw.extend(data)
            self.stream.feed(data)
            if b"\x1b[6n" in data:
                os.write(self.fd, b"\x1b[1;1R")
            if b"\x1b[c" in data:
                os.write(self.fd, b"\x1b[?1;2c")

    def text(self):
        return "\n".join(self.screen.display)

    def input(self, text):
        os.write(self.fd, text.encode())

    def close(self):
        try:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                os.kill(self.pid, signal.SIGTERM)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    break
                self.pump()
            else:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class RemoteServer:
    def __init__(self, socket_path):
        self.socket_path = Path(socket_path)
        self.endpoint = "unix://" + str(self.socket_path)
        self.proc = subprocess.Popen(
            ["codex", "app-server", "--listen", self.endpoint],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while not self.socket_path.exists() and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"remote App Server exited with {self.proc.returncode}")
            time.sleep(.05)
        if not self.socket_path.exists():
            self.close()
            raise TimeoutError("remote App Server Unix socket did not appear")

    @property
    def pid(self):
        return self.proc.pid

    def close(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True, action="store_true")
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--soak-seconds", type=int, default=0)
    p.add_argument("--notify-thread")
    p.add_argument("--run-tag")
    p.add_argument("--model", help="optional model override for the owned TUI")
    p.add_argument("--remote", action="store_true", help="drive the TUI through an owned Unix App Server")
    args = p.parse_args()
    if args.soak_seconds < 0:
        p.error("--soak-seconds must be non-negative")
    if args.notify_thread and not args.soak_seconds:
        p.error("--notify-thread is only valid with --soak-seconds")
    run_tag = args.run_tag or args.report.stem
    if not all(c.isalnum() or c in "_.:@/-" for c in run_tag) or len(run_tag) > 180:
        p.error("--run-tag must be a nonempty event-safe identifier no longer than 180 characters")
    terminal_path = args.report.with_suffix(".terminal.txt")
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
    terminal = server = rpc = remote_server = None
    pool = SessionPool()
    thread = None
    endpoint = "shared-local"
    terminal_buffers = {}
    started = time.monotonic()

    def save_report():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(args.report)

    def write_terminal_buffers():
        temporary = terminal_path.with_suffix(terminal_path.suffix + ".tmp")
        temporary.write_text("\n\n".join(
            f"=== {name} ===\n{value}" for name, value in terminal_buffers.items()
        ))
        temporary.replace(terminal_path)

    def record_step(name, **details):
        step = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        report["steps"].append(step)
        save_report()
        print(json.dumps({"progress": step}), flush=True)

    def capture(name):
        terminal.pump(0)
        terminal_buffers[name] = terminal.text()
        write_terminal_buffers()

    save_report()
    try:
        with tempfile.TemporaryDirectory(prefix="cm-tui-", dir="/tmp") as tmp:
            work = Path(tmp).resolve() / "work"
            work.mkdir()
            command = ["codex"]
            if args.model:
                command += ["--model", args.model]
                report["model"] = args.model
            if args.remote:
                remote_server = RemoteServer(Path(tmp) / "app-server.sock")
                endpoint = remote_server.endpoint
                command += ["--remote", endpoint]
                report["surface"] = "cli-remote"
                report["process_model"]["remote_app_server_pid"] = remote_server.pid
                report["process_model"]["remote_endpoint"] = endpoint
                record_step("remote_app_server_started", pid=remote_server.pid, endpoint=endpoint)
            argv = command + ["-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never",
                    "-c", "model_reasoning_effort=\"low\"", "-c", "tui.animations=false"]
            instruction = "This is an isolated terminal acceptance test. Never use tools, read files, or contact services. " \
                "For each user or external event, reply exactly ACK: followed by its marker, with no spaces. First marker: TUI_BEFORE"
            terminal = Terminal(argv + [instruction])
            report["process_model"]["initial_tui_pid"] = terminal.pid
            record_step("initial_tui_started", tui_pid=terminal.pid, cwd=str(work))
            rpc = Rpc(endpoint)
            last_trust_input = 0.0

            def wait_for(check, timeout=90, label="TUI condition"):
                nonlocal last_trust_input
                until = time.monotonic() + timeout
                while time.monotonic() < until:
                    terminal.pump()
                    # Trust only this empty, disposable directory for this CLI run.
                    screen = terminal.text()
                    now = time.monotonic()
                    if "trust" in screen.lower() and (
                        "Yes, I trust" in screen or "trust this" in screen or "Yes, continue" in screen
                    ) and now - last_trust_input >= .5:
                        terminal.input("\r")
                        last_trust_input = now
                    if check():
                        return
                raise TimeoutError(f"{label} timed out; see terminal buffer artifact")

            def turns():
                return rpc.call(
                    "thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 100}
                )["data"]

            def history_fingerprint():
                return tuple(
                    (turn.get("id"), turn.get("status"), tuple(item.get("id") for item in turn.get("items", [])))
                    for turn in turns()
                )

            def agent_ack_count(marker):
                expected = "ACK:" + marker
                return sum(
                    item.get("type") == "agentMessage" and item.get("text", "").strip() == expected
                    for turn in turns() for item in turn.get("items", [])
                )

            def content_text(item):
                return "\n".join(
                    part.get("text", "") for part in item.get("content", []) if part.get("type") == "text"
                )

            def completed_ack_turn(marker):
                expected = "ACK:" + marker
                return any(
                    turn.get("status") == "completed" and any(
                        item.get("type") == "agentMessage" and item.get("text", "").strip() == expected
                        for item in turn.get("items", [])
                    )
                    for turn in turns()
                )

            def screen_has_agent_ack(marker):
                expected = "ACK:" + marker
                return any(
                    line.strip() in ("• " + expected, "● " + expected)
                    for line in terminal.text().splitlines()
                )

            def screen_is_active(marker):
                screen = terminal.text()
                return "› marker " + marker in screen and "Ask Codex to do anything" not in screen

            def screen_is_idle():
                screen = terminal.text().lower()
                return "ask codex to do anything" in screen and "esc to interrupt" not in screen

            def submit_user(text):
                terminal.input(text)
                for _ in range(5):
                    terminal.pump()
                terminal.input("\r")

            def wait_for_ack(marker, timeout=90, idle_check=None):
                wait_for(lambda: screen_has_agent_ack(marker), timeout, f"rendered agent ACK for {marker}")
                wait_for(
                    lambda: (idle_check or screen_is_idle)() and completed_ack_turn(marker),
                    timeout,
                    f"completed native turn and idle TUI after {marker}",
                )
                if agent_ack_count(marker) != 1:
                    raise AssertionError(f"native history does not contain exactly one agent ACK for {marker}")

            wait_for(lambda: screen_has_agent_ack("TUI_BEFORE"), label="initial rendered agent ACK")
            list_params = {"cwd": str(work), "limit": 10}
            if not args.remote:
                list_params["sourceKinds"] = ["cli"]
            rows = rpc.call("thread/list", list_params)["data"]
            if len(rows) != 1:
                raise AssertionError(f"expected exactly one owned CLI thread, found {len(rows)}")
            thread = rows[0]["id"]
            report["thread"] = thread
            wait_for_ack("TUI_BEFORE")
            capture("before")
            record_step("initial_user_turn_completed", thread=thread, tui_state="idle")
            monitor = Monitor(Path(tmp) / "state", pool)
            monitor.bind("tui", thread, endpoint, ["test"])
            server = Server(monitor, {"test": "isolated-tui-source"}, "isolated-tui-admin", port=0).start()
            report["process_model"]["http_server"] = "in-process at " + server.url
            record_step("in_process_monitor_started", server=server.url)
            event_receipts = []

            def event(marker):
                envelope = {"id": marker, "source": "test", "type": "test.event", "data": {"marker": marker}}
                receipt = send(server.url, "tui", envelope, "isolated-tui-source")
                duplicate = send(server.url, "tui", envelope, "isolated-tui-source")
                if receipt["delivery_id"] != duplicate["delivery_id"]:
                    raise AssertionError("duplicate receipt changed")
                event_receipts.append(receipt["delivery_id"])
                return receipt["delivery_id"]

            delivery = event("TUI_EVENT")
            wait_for_ack("TUI_EVENT")
            capture("event")
            record_step("idle_external_event_completed", delivery_id=delivery)

            submit_user("marker TUI_ACTIVE_USER")
            wait_for(
                lambda: screen_is_active("TUI_ACTIVE_USER"),
                10,
                "deterministic active user turn in owned TUI",
            )
            active_observed_at = datetime.now(timezone.utc).isoformat()
            active_delivery = event("TUI_ACTIVE_EVENT")
            record_step(
                "event_enqueued_during_active_user_turn",
                delivery_id=active_delivery,
                observed_status="active",
                active_observed_at=active_observed_at,
            )
            wait_for_ack("TUI_ACTIVE_USER")
            wait_for_ack("TUI_ACTIVE_EVENT")
            chronological = [item for turn in reversed(turns()) for item in turn.get("items", [])]
            active_client = monitor.event(active_delivery)["client_id"]
            active_user_index = next(
                index for index, item in enumerate(chronological)
                if item.get("type") == "userMessage" and content_text(item).strip() == "marker TUI_ACTIVE_USER"
            )
            active_ack_index = next(
                index for index, item in enumerate(chronological)
                if item.get("type") == "agentMessage" and item.get("text", "").strip() == "ACK:TUI_ACTIVE_USER"
            )
            event_user_index = next(
                index for index, item in enumerate(chronological) if item.get("clientId") == active_client
            )
            event_ack_index = next(
                index for index, item in enumerate(chronological)
                if item.get("type") == "agentMessage" and item.get("text", "").strip() == "ACK:TUI_ACTIVE_EVENT"
            )
            if not active_user_index < active_ack_index < event_user_index < event_ack_index:
                raise AssertionError("native history did not preserve active-user then queued-event order")
            capture("active_event")
            report["checks"]["external_event_queued_during_observed_active_user_turn"] = True

            draft_text = "marker TUI_DRAFT"
            if draft_text in json.dumps(turns()):
                raise AssertionError("draft marker unexpectedly existed before draft test")
            terminal.input(draft_text)
            for _ in range(5):
                terminal.pump()
            if draft_text not in terminal.text():
                raise AssertionError("draft text did not render in the composer")
            draft_delivery = event("TUI_DRAFT_EVENT")
            wait_for_ack("TUI_DRAFT_EVENT", idle_check=lambda: draft_text in terminal.text())
            if draft_text not in terminal.text():
                raise AssertionError("external event erased the unsent TUI draft")
            if draft_text in json.dumps(turns()):
                raise AssertionError("unsent draft appeared in native history before Enter")
            capture("draft_preserved")
            terminal.input("\r")
            wait_for_ack("TUI_DRAFT")
            report["checks"]["unsent_draft_preserved_across_external_event"] = True
            record_step("draft_preserved_and_submitted", delivery_id=draft_delivery)

            submit_user("marker TUI_AFTER")
            wait_for_ack("TUI_AFTER")
            capture("after")
            report["checks"]["user_event_user_visible_in_real_tui"] = True

            idle_before = history_fingerprint()
            idle_until = time.monotonic() + 3
            while time.monotonic() < idle_until:
                terminal.pump(.1)
            if not screen_is_idle() or history_fingerprint() != idle_before:
                raise AssertionError("idle observation changed native turn history")
            report["checks"]["idle_observation_does_not_create_turn"] = True
            record_step("idle_history_stable", observation_seconds=3, turns=len(idle_before))

            terminal.close()
            terminal = None
            record_step("initial_tui_closed_while_idle")
            offline = event("TUI_OFFLINE")
            deadline = time.monotonic() + 20
            while monitor.event(offline)["state"] != "accepted" and time.monotonic() < deadline:
                time.sleep(.1)
            if monitor.event(offline)["state"] != "accepted":
                raise AssertionError("offline event not durably queued")
            # The user client resumes its own task; the monitor never does.
            last_trust_input = 0.0
            terminal = Terminal(argv + ["resume", thread])
            report["process_model"]["resumed_tui_pid"] = terminal.pid
            record_step("tui_resumed_by_user_client", tui_pid=terminal.pid, thread=thread)
            wait_for_ack("TUI_OFFLINE")
            capture("restarted")
            report["checks"]["cli_restart_consumes_offline_event_in_same_thread"] = True

            if args.soak_seconds:
                soak_started = time.monotonic()
                soak_deadline = soak_started + args.soak_seconds
                event_interval = 300 if args.soak_seconds >= 300 else max(2, args.soak_seconds / 3)
                restart_interval = 900 if args.soak_seconds >= 3600 else max(3, args.soak_seconds / 4)
                heartbeat_interval = 30 if args.soak_seconds >= 300 else max(1, args.soak_seconds / 10)
                next_event = soak_started
                next_restart = soak_started + restart_interval
                next_heartbeat = soak_started + heartbeat_interval
                soak_events = 0
                soak_restarts = 0
                idle_fingerprint = history_fingerprint()
                report["soak"] = {
                    "requested_seconds": args.soak_seconds,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "event_interval_seconds": event_interval,
                    "restart_interval_seconds": restart_interval,
                    "heartbeat_interval_seconds": heartbeat_interval,
                    "events": 0,
                    "restarts": 0,
                }
                record_step("soak_started", requested_seconds=args.soak_seconds, tui_pid=terminal.pid)
                while time.monotonic() < soak_deadline:
                    now = time.monotonic()
                    if now >= next_event:
                        marker = f"TUI_SOAK_{soak_events:03d}"
                        receipt = event(marker)
                        wait_for_ack(marker)
                        soak_events += 1
                        report["soak"]["events"] = soak_events
                        idle_fingerprint = history_fingerprint()
                        capture("soak_event_" + str(soak_events))
                        record_step("soak_event_completed", marker=marker, delivery_id=receipt)
                        next_event += event_interval
                        continue
                    if now >= next_restart and next_restart < soak_deadline:
                        before_restart = idle_fingerprint
                        old_pid = terminal.pid
                        terminal.close()
                        terminal = None
                        last_trust_input = 0.0
                        record_step("soak_tui_closed", tui_pid=old_pid, restart=soak_restarts + 1)
                        terminal = Terminal(argv + ["resume", thread])
                        wait_for(
                            screen_is_idle,
                            90,
                            "idle TUI after soak restart",
                        )
                        if history_fingerprint() != before_restart:
                            raise AssertionError("CLI restart created or changed native turn history")
                        soak_restarts += 1
                        report["soak"]["restarts"] = soak_restarts
                        idle_fingerprint = history_fingerprint()
                        capture("soak_restart_" + str(soak_restarts))
                        record_step(
                            "soak_tui_resumed",
                            old_tui_pid=old_pid,
                            tui_pid=terminal.pid,
                            restart=soak_restarts,
                            thread=thread,
                        )
                        next_restart += restart_interval
                        continue
                    if now >= next_heartbeat:
                        if not screen_is_idle() or history_fingerprint() != idle_fingerprint:
                            raise AssertionError("unexpected native turn/history change during soak idle interval")
                        remaining = max(0, soak_deadline - now)
                        record_step(
                            "soak_idle_heartbeat",
                            remaining_seconds=round(remaining, 1),
                            events=soak_events,
                            restarts=soak_restarts,
                            tui_pid=terminal.pid,
                        )
                        next_heartbeat += heartbeat_interval
                        continue
                    terminal.pump(min(.2, max(0, soak_deadline - now)))
                if not screen_is_idle() or history_fingerprint() != idle_fingerprint:
                    raise AssertionError("soak ended with unexpected active state or history change")
                report["soak"]["completed_at"] = datetime.now(timezone.utc).isoformat()
                report["soak"]["actual_seconds"] = round(time.monotonic() - soak_started, 3)
                report["checks"]["wall_clock_soak_completed"] = True
                report["checks"]["soak_idle_history_stable_between_events"] = True
                report["checks"]["soak_restarts_preserve_same_thread_and_history"] = True
                record_step(
                    "soak_completed",
                    actual_seconds=report["soak"]["actual_seconds"],
                    events=soak_events,
                    restarts=soak_restarts,
                )

            all_turns = turns()
            items = [item for turn in all_turns for item in turn.get("items", [])]
            for receipt in event_receipts:
                client = monitor.event(receipt)["client_id"]
                if sum(item.get("clientId") == client for item in items) != 1:
                    raise AssertionError("event history is not exactly one")
            loaded = pool(endpoint).rpc.call("thread/loaded/list", {})["data"]
            if args.remote:
                if thread not in loaded:
                    raise AssertionError("owned remote App Server did not retain the TUI thread")
                report["checks"]["remote_owner_has_exact_tui_thread"] = True
            elif loaded:
                raise AssertionError("writer loaded a thread")
            else:
                report["checks"]["writer_loaded_no_threads"] = True
            if any(turn.get("status") != "completed" for turn in all_turns):
                raise AssertionError("completed acceptance flow left interrupted or active turns")
            report["checks"].update(
                duplicate_history_once=True,
                exact_agent_ack_history_once=True,
                completed_turns_not_interrupted=True,
            )
            report["result"] = "PASS"
            record_step("acceptance_completed", result="PASS", native_turns=len(all_turns))
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        record_step("acceptance_failed", error=report["error"])
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
        if remote_server:
            remote_server.close()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_report()
        if args.notify_thread:
            notify_pool = SessionPool()
            try:
                with tempfile.TemporaryDirectory(prefix="cm-tui-notify-", dir="/tmp") as notify_tmp:
                    notify_monitor = Monitor(Path(notify_tmp) / "state", notify_pool)
                    notify_monitor.bind("result", args.notify_thread, "shared-local", ["test"])
                    envelope = {
                        "id": "tui-soak:" + run_tag,
                        "source": "test",
                        "type": "test.tui_soak_completed" if report["result"] == "PASS" else "test.tui_soak_failed",
                        "data": {
                            "result": report["result"],
                            "report": str(args.report.resolve()),
                            "started_at": report["started_at"],
                            "finished_at": report["finished_at"],
                            "actual_soak_seconds": report.get("soak", {}).get("actual_seconds"),
                            "events": report.get("soak", {}).get("events"),
                            "restarts": report.get("soak", {}).get("restarts"),
                        },
                    }
                    receipt = notify_monitor.ingest("result", envelope)
                    duplicate = notify_monitor.ingest("result", envelope)
                    if receipt["delivery_id"] != duplicate["delivery_id"]:
                        raise AssertionError("result notification duplicate receipt changed")
                    deadline = time.monotonic() + 20
                    while notify_monitor.event(receipt["delivery_id"])["state"] != "accepted":
                        if time.monotonic() >= deadline:
                            raise TimeoutError("result notification was not accepted")
                        notify_monitor.dispatch_once()
                        time.sleep(.1)
                    if notify_pool("shared-local").rpc.call("thread/loaded/list", {})["data"]:
                        raise AssertionError("result notification writer loaded a thread")
                    report["notification"] = {
                        "state": "accepted",
                        "thread": args.notify_thread,
                        "event_id": envelope["id"],
                        "delivery_id": receipt["delivery_id"],
                    }
            except Exception as exc:
                report["notification"] = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
            finally:
                notify_pool.close()
            save_report()
        if report["result"] == "FAIL":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            failure_report = args.report.with_name(args.report.stem + ".failure-" + stamp + args.report.suffix)
            failure_terminal = terminal_path.with_name(terminal_path.stem + ".failure-" + stamp + terminal_path.suffix)
            report["preserved_failure_report"] = str(failure_report)
            report["preserved_failure_terminal_buffer"] = str(failure_terminal)
            save_report()
            failure_report.write_text(args.report.read_text())
            if terminal_path.exists():
                failure_terminal.write_text(terminal_path.read_text())
        print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
