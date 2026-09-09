#!/usr/bin/env python3
"""Bounded resident App Server lifecycle canary in an owned real TUI.

The canary starts one disposable Unix App Server and one ordinary Codex TUI,
then keeps the saved thread subscribed through ``ResidentKeeper`` while the
TUI is closed.  It sends only events addressed to the canary's own thread,
checks native history for one exact client message and one exact model reply,
and exercises resident reconnect after the owned App Server is restarted.

This is an opt-in model-backed check.  The TUI uses read-only/never approval
settings.  The only input this harness sends automatically is the workspace
trust response for its temporary workspace; it never answers command,
approval, or elicitation requests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_monitor.http import Server
from codex_monitor.monitor import Monitor
from codex_monitor.resident import ResidentKeeper
from codex_monitor.session import Rpc, SessionPool
from codex_monitor.cli import send


MODEL = "gpt-5.6-luna"
REASONING = "xhigh"


def import_tui_helpers():
    path = ROOT / "scripts" / "tui-canary.py"
    spec = importlib.util.spec_from_file_location("resident_tui_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load PTY helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Terminal, module.RemoteServer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report = {
            "result": "RUNNING",
            "scope": "owned Unix App Server / ordinary Codex TUI / resident connection",
            "evidence_kind": (
                "native thread history and rendered PTY output from a disposable owned endpoint; "
                "no Desktop or original-incident claim"
            ),
            "model": args.model,
            "reasoning_effort": REASONING,
            "approval_policy": "never",
            "workspace_trust_policy": "only the temporary canary workspace",
            "checks": {},
            "steps": [],
            "started_at": utc_now(),
            "process_model": {"harness_pid": os.getpid()},
        }
        self.report_path = args.report.resolve()
        self.terminal = None
        self.second_terminal = None
        self.owner = None
        self.observer = None
        self.pool = SessionPool()
        self.monitor_server = None
        self.keeper = None
        self.keeper_stop = None
        self.keeper_thread = None
        self.thread = None
        self.second_thread = None
        self.threads = []
        self.workspace = None
        self.tempdir = None
        self.endpoint = None
        self.monitor = None
        self.started_trust_inputs = 0
        self.last_trust_input = 0.0
        self.trust_sent = False
        self.trust_prompt_seen = False
        self.terminal_buffers = {}
        self.terminal_buffer_path = self.report_path.with_suffix(".terminal.txt")

    def save(self):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.report["updated_at"] = utc_now()
        temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(self.report_path)

    def step(self, name: str, **details):
        value = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "at": utc_now(),
            **details,
        }
        self.report["steps"].append(value)
        self.save()
        print(json.dumps({"progress": value}, ensure_ascii=False), flush=True)

    def check(self, name: str, condition: bool, **details):
        if not condition:
            raise AssertionError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    def wait_for(self, condition, label: str, timeout: float | None = None, *, pump=True):
        deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
        last_error = None
        while time.monotonic() < deadline:
            if pump and self.terminal is not None:
                self.pump_terminal()
            try:
                value = condition()
                if value:
                    return value
            except (OSError, RuntimeError) as exc:
                last_error = exc
            time.sleep(.05)
        suffix = f" ({last_error})" if last_error else ""
        raise TimeoutError(f"{label} timed out{suffix}")

    def pump_terminal(self):
        self.terminal.pump(.05)
        screen = self.terminal.text()
        lower = screen.lower()
        approval_prompt = (
            "would you like to run the following command" in lower
            or "allow command" in lower
            or ("approve this command" in lower and "deny" in lower)
        )
        # Reject an approval prompt before considering any key input.  The
        # canary never answers command, permission, or elicitation requests.
        if approval_prompt:
            raise AssertionError("unexpected command approval prompt")
        trusted_workspace = self.workspace is not None and str(self.workspace).lower() in lower
        trust_modal = (
            "trust this folder" in lower
            or "trust the contents" in lower
            or "trust this workspace" in lower
            or "do you trust" in lower
        )
        trust_choice = "yes, i trust" in lower or "yes, continue" in lower
        if trusted_workspace and trust_modal and trust_choice:
            self.trust_prompt_seen = True
        # Only an exact trust modal naming this run's workspace can receive
        # one Enter, and one Enter is allowed per TUI process.
        if (
            not self.trust_sent
            and trusted_workspace
            and trust_modal
            and trust_choice
        ):
            self.terminal.input("\r")
            self.trust_sent = True
            self.started_trust_inputs += 1

    def launch_terminal(self, argv, label: str):
        self.trust_sent = False
        self.trust_prompt_seen = False
        self.last_trust_input = 0.0
        self.terminal = self.Terminal(argv)
        self.report["process_model"][label + "_tui_pid"] = self.terminal.pid

    def capture_terminal(self, name: str):
        if self.terminal is None:
            return
        self.terminal_buffers[name] = self.terminal.text()
        encoded = "\n\n".join(
            f"=== {label} ===\n{value}" for label, value in self.terminal_buffers.items()
        )
        temporary = self.terminal_buffer_path.with_suffix(self.terminal_buffer_path.suffix + ".tmp")
        temporary.write_text(encoded)
        temporary.replace(self.terminal_buffer_path)
        self.report["terminal_buffer_capture"] = str(self.terminal_buffer_path)
        self.save()

    @staticmethod
    def history(rpc: Rpc, thread: str):
        return rpc.call(
            "thread/turns/list",
            {"threadId": thread, "itemsView": "full", "limit": 100},
            timeout=5,
        )["data"]

    @staticmethod
    def user_items(turns, client_id):
        return [
            item
            for turn in turns
            for item in turn.get("items", [])
            if item.get("type") == "userMessage" and item.get("clientId") == client_id
        ]

    @staticmethod
    def agent_acks(turns, marker):
        expected = "ACK:" + marker
        return [
            item
            for turn in turns
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and item.get("text", "").strip() == expected
        ]

    @staticmethod
    def screen_has_ack(terminal, marker):
        expected = "ACK:" + marker
        return any(
            line.strip() in {expected, "• " + expected, "● " + expected}
            for line in terminal.text().splitlines()
        )

    @staticmethod
    def screen_is_idle(terminal):
        screen = terminal.text().lower()
        return "ask codex to do anything" in screen and "esc to interrupt" not in screen

    def wait_ack(self, marker, *, client_id=None, thread=None):
        thread = thread or self.thread
        def observed():
            turns = self.history(self.observer, thread)
            acks = self.agent_acks(turns, marker)
            users = self.user_items(turns, client_id) if client_id else []
            return turns if acks and (client_id is None or users) else None

        turns = self.wait_for(observed, f"native ACK for {marker}", pump=self.terminal is not None)
        self.wait_for(
            lambda: any(
                turn.get("status") == "completed" and self.agent_acks([turn], marker)
            for turn in self.history(self.observer, thread)
            ),
            f"completed native turn for {marker}",
            pump=self.terminal is not None,
        )
        if self.terminal is not None:
            self.wait_for(
                lambda: self.screen_has_ack(self.terminal, marker),
                f"rendered ACK for {marker}",
            )
            self.wait_for(
                lambda: self.screen_is_idle(self.terminal),
                f"idle TUI after {marker}",
            )
        final = self.history(self.observer, thread)
        if len(self.agent_acks(final, marker)) != 1:
            raise AssertionError(f"native history contains multiple ACKs for {marker}")
        if client_id is not None and len(self.user_items(final, client_id)) != 1:
            raise AssertionError(f"native history contains multiple event messages for {marker}")
        return final

    def submit_user(self, text: str):
        self.terminal.input(text)
        self.wait_for(
            lambda: text in self.terminal.text(),
            "user followup rendered in composer",
            timeout=15,
        )
        time.sleep(.25)
        for _ in range(4):
            self.terminal.pump()
        self.terminal.input("\r")

    def queue_event(self, marker: str, event_id: str, *, binding="resident", wait_accept=True):
        envelope = {
            "id": event_id,
            "source": "resident-canary",
            "type": "resident.test_event",
            "data": {"message": "marker " + marker, "marker": marker},
        }
        first = send(
            self.monitor_server.url,
            binding,
            envelope,
            "resident-canary-source",
        )
        duplicate = send(
            self.monitor_server.url,
            binding,
            envelope,
            "resident-canary-source",
        )
        if first["delivery_id"] != duplicate["delivery_id"] or not duplicate.get("duplicate"):
            raise AssertionError("stable event ID did not deduplicate to one receipt")
        delivery_id = first["delivery_id"]
        if wait_accept:
            self.wait_for(
                lambda: self.monitor.event(delivery_id)["state"] == "accepted",
                f"monitor acceptance for {marker}",
                pump=False,
            )
        event = self.monitor.event(delivery_id)
        return delivery_id, event["client_id"]

    def record_event_audit(self, label: str, delivery_id: str, client_id: str, marker: str, thread: str):
        turns = self.history(self.observer, thread)
        self.report.setdefault("event_audit", {})[label] = {
            "delivery_id": delivery_id,
            "client_id": client_id,
            "local_state": self.monitor.event(delivery_id)["state"],
            "native_user_occurrences": len(self.user_items(turns, client_id)),
            "exact_agent_ack_occurrences": len(self.agent_acks(turns, marker)),
            "thread": thread,
        }

    def start_keeper(self):
        self.keeper = ResidentKeeper(
            self.endpoint,
            self.threads or [self.thread],
            health_interval=.25,
            backoff_initial=.2,
            backoff_max=1.0,
        )
        self.keeper_stop = threading.Event()
        self.keeper_thread = threading.Thread(
            target=self.keeper.run,
            args=(self.keeper_stop,),
            name="resident-canary-keeper",
            daemon=True,
        )
        self.keeper_thread.start()
        self.wait_for(
            lambda: self.keeper.status()["connected"] and self.keeper.status()["subscribed"],
            "resident thread/resume subscription",
            pump=False,
        )

    def stop_keeper(self):
        if self.keeper_stop is not None:
            self.keeper_stop.set()
        if self.keeper_thread is not None:
            self.keeper_thread.join(timeout=5)
            if self.keeper_thread.is_alive():
                raise RuntimeError("resident keeper worker did not stop before workspace cleanup")
        self.keeper_thread = None

    def restart_owner(self, socket_path: Path):
        old_pid = self.owner.pid if self.owner is not None else self.report["process_model"].get("owner_pid_before_restart")
        if self.owner is not None:
            self.owner.close()
            self.owner = None
        if self.keeper is not None:
            self.wait_for(
                lambda: not self.keeper.status()["connected"],
                "resident disconnect during owner restart",
                timeout=10,
                pump=False,
            )
        deadline = time.monotonic() + 5
        while socket_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if socket_path.exists():
            socket_path.unlink()
        self.owner = self.RemoteServer(socket_path)
        self.report["process_model"]["owner_restart_pid"] = self.owner.pid
        self.check("owner_server_pid_changed", self.owner.pid != old_pid, old_pid=old_pid, new_pid=self.owner.pid)

    def stop_owner(self, socket_path: Path):
        if self.owner is None:
            return
        self.report["process_model"]["owner_pid_before_restart"] = self.owner.pid
        self.owner.close()
        self.owner = None
        deadline = time.monotonic() + 5
        while socket_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if socket_path.exists():
            socket_path.unlink()
        if self.keeper is not None:
            self.wait_for(
                lambda: not self.keeper.status()["connected"],
                "resident disconnect while owner is down",
                timeout=10,
                pump=False,
            )

    def run(self):
        self.Terminal, self.RemoteServer = import_tui_helpers()
        self.tempdir = tempfile.TemporaryDirectory(prefix="codex-resident-canary-", dir="/tmp")
        root = Path(self.tempdir.name)
        work = (root / "workspace").resolve()
        work.mkdir()
        socket_path = root / "app-server.sock"
        self.workspace = work
        self.report["runtime"] = {"temporary_root": str(root), "workspace": str(work)}
        self.owner = self.RemoteServer(socket_path)
        self.endpoint = self.owner.endpoint
        self.report["process_model"].update(
            owner_pid=self.owner.pid,
            owner_endpoint=self.endpoint,
        )
        self.step("owned_app_server_started", pid=self.owner.pid, endpoint=self.endpoint)

        command = [
            "codex", "--model", self.args.model, "--remote", self.endpoint,
            "-C", str(work), "--no-alt-screen", "-s", "read-only", "-a", "never",
            "-c", f'model_reasoning_effort="{REASONING}"',
            "-c", "tui.animations=false",
        ]
        instruction = (
            "This is an isolated resident lifecycle acceptance test. Never use tools, read files, "
            "or contact services. For every user message and every external event, reply exactly "
            "ACK:<marker> with no additional words. The marker is the uppercase token in the "
            "message or event data. First marker: RESIDENT_INITIAL"
        )
        self.launch_terminal(command + [instruction], "initial")
        self.step("ordinary_tui_started", tui_pid=self.terminal.pid, workspace=str(work))
        self.observer = Rpc(self.endpoint)

        def discover_initial_thread():
            rows = self.observer.call(
                "thread/list", {"cwd": str(work), "limit": 20}, timeout=5
            )["data"]
            if len(rows) != 1:
                return False
            self.thread = rows[0]["id"]
            self.threads = [self.thread]
            self.report["thread"] = self.thread
            return True

        self.wait_for(discover_initial_thread, "owned remote TUI thread discovery")
        self.wait_for(
            lambda: self.screen_has_ack(self.terminal, "RESIDENT_INITIAL"),
            "initial ordinary TUI ACK",
        )
        self.wait_for(lambda: self.screen_is_idle(self.terminal), "initial TUI idle")
        initial_turns = self.history(self.observer, self.thread)
        if len(self.agent_acks(initial_turns, "RESIDENT_INITIAL")) != 1:
            raise AssertionError("initial model ACK was not exact once in native history")
        self.check("initial_tui_turn_completed", True, native_turns=len(initial_turns))

        # Create a second ordinary TUI task on the same owned server before
        # starting the resident, so one subscription owner can retain both
        # explicitly selected conversations while both UI clients are closed.
        first_terminal = self.terminal
        second_instruction = (
            "This is the second isolated resident lifecycle task. Never use tools, read files, "
            "or contact services. For every user message and every external event, reply exactly "
            "ACK:<marker> with no additional words. The marker is the uppercase token in the "
            "message or event data. First marker: RESIDENT_SECOND_INITIAL"
        )
        self.launch_terminal(command + [second_instruction], "second")
        self.second_terminal = self.terminal
        def discover_second_thread():
            second_rows = self.observer.call(
                "thread/list", {"cwd": str(work), "limit": 20}, timeout=5
            )["data"]
            if len(second_rows) != 2:
                return False
            candidates = [row["id"] for row in second_rows if row["id"] != self.thread]
            if len(candidates) != 1:
                return False
            self.second_thread = candidates[0]
            self.threads.append(self.second_thread)
            return True

        self.wait_for(discover_second_thread, "second owned remote TUI thread discovery")
        self.wait_for(
            lambda: self.screen_has_ack(self.second_terminal, "RESIDENT_SECOND_INITIAL"),
            "second ordinary TUI ACK",
        )
        self.wait_for(lambda: self.screen_is_idle(self.second_terminal), "second TUI idle")
        second_turns = self.history(self.observer, self.second_thread)
        if len(self.agent_acks(second_turns, "RESIDENT_SECOND_INITIAL")) != 1:
            raise AssertionError("second model ACK was not exact once in native history")
        self.check("second_tui_task_created_and_completed", True, thread=self.second_thread)
        self.terminal = first_terminal

        self.monitor = Monitor(root / "monitor-state", self.pool)
        self.monitor.bind("resident", self.thread, self.endpoint, ["resident-canary"])
        self.monitor.bind("resident-second", self.second_thread, self.endpoint, ["resident-canary"])
        self.monitor_server = Server(
            self.monitor,
            {"resident-canary": "resident-canary-source"},
            "resident-canary-admin",
            port=0,
        ).start()
        self.report["process_model"]["monitor_http"] = self.monitor_server.url
        self.step("owned_monitor_started", url=self.monitor_server.url)

        self.start_keeper()
        self.check("resident_subscribed_before_tui_close", True, thread=self.thread)

        self.capture_terminal("initial")
        self.terminal = self.second_terminal
        self.capture_terminal("second-initial")
        self.terminal = first_terminal
        self.second_terminal.close()
        self.second_terminal = None
        self.terminal.close()
        self.terminal = None
        self.step("ordinary_tuis_closed_while_resident_subscribed", threads=self.threads)

        delivery, client_id = self.queue_event(
            "RESIDENT_OFFLINE", "resident-offline-event-v1"
        )
        self.report["offline_delivery_id"] = delivery
        self.wait_ack("RESIDENT_OFFLINE", client_id=client_id)
        self.record_event_audit("resident_offline", delivery, client_id, "RESIDENT_OFFLINE", self.thread)
        self.check(
            "resident_consumed_event_while_tui_closed_exactly_once",
            len(self.user_items(self.history(self.observer, self.thread), client_id)) == 1,
            delivery_id=delivery,
        )
        second_delivery, second_client_id = self.queue_event(
            "RESIDENT_SECOND_OFFLINE", "resident-second-offline-event-v1", binding="resident-second"
        )
        self.report["second_offline_delivery_id"] = second_delivery
        self.wait_ack("RESIDENT_SECOND_OFFLINE", client_id=second_client_id, thread=self.second_thread)
        self.record_event_audit(
            "resident_second_offline", second_delivery, second_client_id,
            "RESIDENT_SECOND_OFFLINE", self.second_thread,
        )
        self.check(
            "second_resident_event_consumed_independently_exactly_once",
            len(self.user_items(self.history(self.observer, self.second_thread), second_client_id)) == 1,
            delivery_id=second_delivery,
        )

        self.launch_terminal(command + ["resume", self.thread], "resumed")
        self.step("same_thread_tui_reopened", tui_pid=self.terminal.pid, thread=self.thread)
        self.wait_for(
            lambda: self.screen_has_ack(self.terminal, "RESIDENT_OFFLINE"),
            "reopened TUI replay of resident event",
        )
        self.wait_for(lambda: self.screen_is_idle(self.terminal), "reopened TUI idle")
        self.submit_user("marker RESIDENT_USER_AFTER")
        self.wait_ack("RESIDENT_USER_AFTER")
        self.check("reopened_tui_user_followup_completed", True)
        self.capture_terminal("resumed")
        self.terminal.close()
        self.terminal = None
        self.step("reopened_tui_closed_before_owner_restart")

        old_owner_pid = self.owner.pid
        old_connected_at = self.keeper.status().get("connected_at")
        resident_rpc = self.keeper._rpc
        if resident_rpc is None:
            raise AssertionError("resident had no RPC connection to disconnect")
        resident_rpc.close()
        self.wait_for(
            lambda: not self.keeper.status()["connected"],
            "resident RPC disconnect observation",
            pump=False,
        )
        self.check("resident_rpc_disconnect_forced", True)
        self.wait_for(
            lambda: self.keeper.status()["connected"] and self.keeper.status()["subscribed"],
            "resident transport reconnect with owner still running",
            pump=False,
        )
        same_owner_status = self.keeper.status()
        same_owner_connected_at = same_owner_status.get("connected_at")
        self.check(
            "resident_reconnected_without_owner_restart",
            self.owner.pid == old_owner_pid and same_owner_connected_at != old_connected_at,
            owner_pid=self.owner.pid,
            connected_at_before=old_connected_at,
            connected_at_after=same_owner_connected_at,
        )

        outage_started = time.monotonic()
        self.stop_owner(socket_path)
        outage_delivery, outage_client_id = self.queue_event(
            "RESIDENT_OWNER_DOWN", "resident-owner-down-event-v1", wait_accept=False
        )
        self.report["owner_down_delivery_id"] = outage_delivery
        self.wait_for(
            lambda: (
                self.monitor.event(outage_delivery)["state"] == "pending" and
                self.monitor.event(outage_delivery)["attempts"] == 0 and
                bool(self.monitor.event(outage_delivery)["error"])
            ),
            "owner-down event returned to pending without attempt budget",
            timeout=30,
            pump=False,
        )
        outage_deadline = time.monotonic() + 20
        while time.monotonic() < outage_deadline:
            event = self.monitor.event(outage_delivery)
            # A dispatch transaction temporarily reserves one attempt while
            # its connectivity check is in flight. Judge the settled pending
            # state, not that transient reservation.
            if event["state"] not in ("pending", "submitting") or (
                event["state"] == "pending" and event["attempts"] != 0
            ):
                raise AssertionError("owner-down event consumed retry budget before owner recovery")
            time.sleep(.2)
        def settled_outage_event():
            event = self.monitor.event(outage_delivery)
            return event if event["state"] == "pending" else None

        outage_event = self.wait_for(
            settled_outage_event,
            "settled owner-down delivery state", timeout=15, pump=False,
        )
        self.check(
            "owner_down_event_retained_pending_without_attempt_budget",
            outage_event["state"] == "pending" and outage_event["attempts"] == 0 and bool(outage_event["error"]),
            outage_seconds=round(time.monotonic() - outage_started, 1),
            attempts=outage_event["attempts"],
            state=outage_event["state"],
        )

        self.restart_owner(socket_path)
        self.wait_for(
            lambda: self.keeper.status()["connected"] and self.keeper.status()["subscribed"],
            "resident re-registration after owner restart",
            pump=False,
        )
        new_connected_at = self.keeper.status().get("connected_at")
        self.check(
            "resident_reregistered_after_owner_restart",
            same_owner_connected_at != new_connected_at,
            connected_at_before=same_owner_connected_at,
            connected_at_after=new_connected_at,
        )

        self.observer.close()
        self.observer = Rpc(self.endpoint)
        loaded = self.observer.call("thread/loaded/list", {}, timeout=5)["data"]
        self.check("restarted_owner_loaded_same_threads", set(self.threads).issubset(set(loaded)))
        self.wait_for(
            lambda: self.monitor.event(outage_delivery)["state"] == "accepted",
            "owner-down event accepted after owner restart",
            pump=False,
        )
        self.wait_ack("RESIDENT_OWNER_DOWN", client_id=outage_client_id)
        self.record_event_audit(
            "resident_owner_down", outage_delivery, outage_client_id,
            "RESIDENT_OWNER_DOWN", self.thread,
        )
        self.check(
            "owner_down_event_consumed_exactly_once_after_restart",
            len(self.user_items(self.history(self.observer, self.thread), outage_client_id)) == 1,
            delivery_id=outage_delivery,
        )
        delivery, client_id = self.queue_event(
            "RESIDENT_AFTER_RESTART", "resident-after-restart-event-v1"
        )
        self.report["restart_delivery_id"] = delivery
        self.wait_ack("RESIDENT_AFTER_RESTART", client_id=client_id)
        self.record_event_audit("resident_after_restart", delivery, client_id, "RESIDENT_AFTER_RESTART", self.thread)
        final_turns = self.history(self.observer, self.thread)
        self.check(
            "post_restart_event_consumed_exactly_once",
            len(self.user_items(final_turns, client_id)) == 1 and
            len(self.agent_acks(final_turns, "RESIDENT_AFTER_RESTART")) == 1,
            delivery_id=delivery,
        )
        self.report["functional_checks_complete"] = True
        self.step("resident_lifecycle_functional_checks_complete", result="FUNCTIONAL_CHECKS_COMPLETE")

    def cleanup(self):
        errors = []
        if self.terminal is not None:
            try:
                self.capture_terminal("failure")
                self.terminal.close()
            except Exception as exc:
                errors.append(f"close_tui: {type(exc).__name__}: {exc}")
            self.terminal = None
            if self.second_terminal is not None:
                self.second_terminal = None
        elif self.second_terminal is not None:
            try:
                self.terminal = self.second_terminal
                self.capture_terminal("second-failure")
                self.second_terminal.close()
            except Exception as exc:
                errors.append(f"close_second_tui: {type(exc).__name__}: {exc}")
            self.second_terminal = None
        try:
            self.stop_keeper()
        except Exception as exc:
            errors.append(f"stop_resident: {type(exc).__name__}: {exc}")
        # Close all normal observer clients before opening one short-lived
        # connection for archiving the canary's own saved thread.
        if self.observer is not None:
            try:
                self.observer.close()
            except Exception as exc:
                errors.append(f"close_observer: {type(exc).__name__}: {exc}")
            self.observer = None
        archive_threads = list(dict.fromkeys(self.threads or ([self.thread] if self.thread else [])))
        if (
            archive_threads and self.endpoint and self.owner is None and
            self.RemoteServer is not None and self.keeper_thread is None
        ):
            try:
                socket_path = Path(self.endpoint.removeprefix("unix://"))
                if socket_path.exists():
                    socket_path.unlink()
                self.owner = self.RemoteServer(socket_path)
            except Exception as exc:
                errors.append(f"restart_owner_for_archive: {type(exc).__name__}: {exc}")
        if archive_threads and self.endpoint and self.owner is not None:
            for thread in archive_threads:
                archive_rpc = None
                try:
                    archive_rpc = Rpc(self.endpoint)
                    archive_rpc.call("thread/archive", {"threadId": thread}, timeout=5)
                    self.report["checks"].setdefault("owned_threads_archived", []).append(thread)
                except Exception as exc:
                    errors.append(f"archive_owned_thread:{thread}: {type(exc).__name__}: {exc}")
                finally:
                    if archive_rpc is not None:
                        archive_rpc.close()
        if self.monitor_server is not None:
            try:
                self.monitor_server.close()
            except Exception as exc:
                errors.append(f"close_monitor: {type(exc).__name__}: {exc}")
            self.monitor_server = None
        try:
            self.pool.close()
        except Exception as exc:
            errors.append(f"close_pool: {type(exc).__name__}: {exc}")
        if self.owner is not None:
            try:
                self.owner.close()
            except Exception as exc:
                errors.append(f"close_owner: {type(exc).__name__}: {exc}")
            self.owner = None
        if self.started_trust_inputs > 1:
            errors.append("workspace trust was answered more than once")
        self.report["workspace_trust_inputs"] = self.started_trust_inputs
        if errors:
            self.report["cleanup_errors"] = errors
            self.report["result"] = "FAIL"

    def dispose_workspace(self):
        if self.keeper_thread is not None and self.keeper_thread.is_alive():
            raise RuntimeError("refusing to remove canary workspace while resident worker is alive")
        if self.owner is not None:
            raise RuntimeError("refusing to remove canary workspace while owner server is alive")
        if self.tempdir is not None:
            self.tempdir.cleanup()
            self.tempdir = None

    def finish(self):
        self.report["finished_at"] = utc_now()
        self.save()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    canary = Canary(args)
    run_ok = False
    cleanup_ok = False
    dispose_ok = False
    canary.save()
    try:
        canary.run()
        run_ok = bool(canary.report.get("functional_checks_complete"))
    except BaseException as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        canary.step("resident_lifecycle_acceptance_failed", error=canary.report["error"])
    finally:
        try:
            canary.cleanup()
            cleanup_ok = not canary.report.get("cleanup_errors")
        except BaseException as exc:
            canary.report["result"] = "FAIL"
            canary.report["cleanup_errors"] = [
                f"cleanup: {type(exc).__name__}: {exc}"
            ]
        try:
            canary.dispose_workspace()
            dispose_ok = True
        except BaseException as exc:
            canary.report["result"] = "FAIL"
            canary.report.setdefault("cleanup_errors", []).append(
                f"dispose_workspace: {type(exc).__name__}: {exc}"
            )
        if run_ok and cleanup_ok and dispose_ok:
            canary.report["result"] = "PASS"
        else:
            canary.report["result"] = "FAIL"
        canary.finish()
    return 0 if canary.report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
