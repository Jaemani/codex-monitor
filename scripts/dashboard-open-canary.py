#!/usr/bin/env python3
"""Bounded dashboard-to-Codex open canary in disposable owned fixtures.

The canary creates one disposable Unix App Server and one ordinary Codex TUI
conversation, closes the TUI, and exposes that exact conversation through a
read-only dashboard binding.  It presses Enter on the selected dashboard row,
checks a user follow-up in the same native history, quits the opened TUI, and
checks that the dashboard returns.  It never contacts an existing service or
conversation and archives its temporary thread before reporting PASS.
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
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_monitor.monitor import Monitor  # noqa: E402
from codex_monitor.session import Rpc  # noqa: E402


MODEL = "gpt-5.6-luna"
REASONING = "xhigh"
INITIAL_MARKER = "DASHBOARD_OPEN_INITIAL"
FOLLOWUP_MARKER = "DASHBOARD_OPEN_FOLLOWUP"


def import_tui_helpers():
    dashboard_path = ROOT / "scripts" / "dashboard-canary.py"
    dashboard_spec = importlib.util.spec_from_file_location("dashboard_open_dashboard_helpers", dashboard_path)
    if dashboard_spec is None or dashboard_spec.loader is None:
        raise RuntimeError(f"cannot load PTY helpers from {dashboard_path}")
    dashboard_module = importlib.util.module_from_spec(dashboard_spec)
    dashboard_spec.loader.exec_module(dashboard_module)

    tui_path = ROOT / "scripts" / "tui-canary.py"
    tui_spec = importlib.util.spec_from_file_location("dashboard_open_tui_helpers", tui_path)
    if tui_spec is None or tui_spec.loader is None:
        raise RuntimeError(f"cannot load owner helper from {tui_path}")
    tui_module = importlib.util.module_from_spec(tui_spec)
    tui_spec.loader.exec_module(tui_module)
    return dashboard_module.Terminal, tui_module.RemoteServer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report: dict[str, Any] = {
            "result": "RUNNING",
            "scope": "disposable dashboard PTY / owned Unix App Server / ordinary Codex TUI",
            "evidence_kind": (
                "rendered dashboard and TUI PTY output plus exact native history from a disposable fixture; "
                "no Desktop or existing-service claim"
            ),
            "model": args.model,
            "reasoning_effort": REASONING,
            "approval_policy": "never",
            "checks": {},
            "steps": [],
            "started_at": utc_now(),
            "process_model": {"harness_pid": os.getpid()},
        }
        self.report_path = args.report.resolve()
        self.terminal_class = None
        self.remote_server_class = None
        self.owner = None
        self.observer: Rpc | None = None
        self.dashboard = None
        self.initial_tui = None
        self.endpoint: str | None = None
        self.thread: str | None = None
        self.workspace: Path | None = None
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.state: Path | None = None
        self.monitor: Monitor | None = None
        self.trust_sent = False
        self.terminal_buffers: dict[str, str] = {}
        self.terminal_buffer_path = self.report_path.with_suffix(".terminal.txt")

    def save(self) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.report["updated_at"] = utc_now()
        temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(self.report_path)

    def step(self, name: str, **details: Any) -> None:
        value = {
            "name": name,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "at": utc_now(),
            **details,
        }
        self.report["steps"].append(value)
        self.save()
        print(json.dumps({"progress": value}, ensure_ascii=False), flush=True)

    def check(self, name: str, condition: bool, **details: Any) -> None:
        if not condition:
            raise AssertionError(name)
        self.report["checks"][name] = True
        self.step(name, **details)

    @staticmethod
    def history(rpc: Rpc, thread: str) -> list[dict[str, Any]]:
        return rpc.call(
            "thread/turns/list",
            {"threadId": thread, "itemsView": "full", "limit": 100},
            timeout=5,
        )["data"]

    @staticmethod
    def agent_acks(turns: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
        expected = "ACK:" + marker
        return [
            item
            for turn in turns
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and item.get("text", "").strip() == expected
        ]

    @staticmethod
    def user_markers(turns: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
        def contains_marker(item: dict[str, Any]) -> bool:
            if marker in str(item.get("text", "")):
                return True
            content = item.get("content")
            if not isinstance(content, list):
                return False
            return any(
                isinstance(part, dict) and marker in str(part.get("text", ""))
                for part in content
            )

        return [
            item
            for turn in turns
            for item in turn.get("items", [])
            if item.get("type") == "userMessage" and contains_marker(item)
        ]

    @staticmethod
    def screen_has_ack(terminal, marker: str) -> bool:
        expected = "ACK:" + marker
        return any(
            line.strip() in {expected, "• " + expected, "● " + expected}
            for line in terminal.text().splitlines()
        )

    @staticmethod
    def screen_is_idle(terminal) -> bool:
        screen = terminal.text().lower()
        return "ask codex to do anything" in screen and "esc to interrupt" not in screen

    def pump_tui(self, terminal) -> None:
        terminal.pump(.05)
        screen = terminal.text()
        lower = screen.lower()
        if (
            "would you like to run the following command" in lower
            or "allow command" in lower
            or ("approve this command" in lower and "deny" in lower)
        ):
            raise AssertionError("unexpected command approval prompt")
        workspace = str(self.workspace).lower() if self.workspace else ""
        trust_modal = (
            "trust this folder" in lower
            or "trust the contents" in lower
            or "trust this workspace" in lower
            or "do you trust" in lower
        )
        trust_choice = "yes, i trust" in lower or "yes, continue" in lower
        if not self.trust_sent and workspace and workspace in lower and trust_modal and trust_choice:
            terminal.send("\r")
            self.trust_sent = True

    def wait_for(self, terminal, predicate, label: str, timeout: float | None = None):
        deadline = time.monotonic() + (self.args.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            if terminal is not None:
                self.pump_tui(terminal)
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        raise TimeoutError(f"{label} timed out")

    def capture(self, name: str, terminal) -> None:
        if terminal is None:
            return
        terminal.pump(0)
        self.terminal_buffers[name] = terminal.text()
        encoded = "\n\n".join(
            f"=== {label} ===\n{value}" for label, value in self.terminal_buffers.items()
        )
        temporary = self.terminal_buffer_path.with_suffix(self.terminal_buffer_path.suffix + ".tmp")
        temporary.write_text(encoded)
        temporary.replace(self.terminal_buffer_path)
        self.report["terminal_buffer_capture"] = str(self.terminal_buffer_path)
        self.save()

    def command(self, instruction: str) -> list[str]:
        assert self.endpoint is not None and self.workspace is not None
        return [
            "codex", "--model", self.args.model, "--remote", self.endpoint,
            "-C", str(self.workspace), "--no-alt-screen", "-s", "read-only", "-a", "never",
            "-c", f'model_reasoning_effort="{REASONING}"',
            "-c", "tui.animations=false", instruction,
        ]

    def run(self) -> None:
        self.terminal_class, self.remote_server_class = import_tui_helpers()
        self.tempdir = tempfile.TemporaryDirectory(prefix="codex-dashboard-open-", dir="/tmp")
        root = Path(self.tempdir.name)
        self.workspace = (root / "workspace").resolve()
        self.workspace.mkdir()
        self.state = root / "monitor-state"
        self.state.mkdir(mode=0o700)
        socket_path = root / "app-server.sock"
        self.report["runtime"] = {
            "temporary_root": str(root),
            "workspace": str(self.workspace),
            "state": str(self.state),
        }

        self.owner = self.remote_server_class(socket_path)
        self.endpoint = self.owner.endpoint
        self.report["process_model"].update({
            "owner_pid": self.owner.pid,
            "owner_endpoint": self.endpoint,
        })
        self.step("owned_app_server_started", pid=self.owner.pid, endpoint=self.endpoint)

        instruction = (
            "This is an isolated dashboard-open acceptance test. Never use tools, read files, or contact services. "
            "For each user message, reply exactly ACK:<marker> with no additional words. "
            f"First marker: {INITIAL_MARKER}"
        )
        self.trust_sent = False
        self.initial_tui = self.terminal_class(
            self.command(instruction), width=120, height=40, cwd=self.workspace
        )
        self.report["process_model"]["initial_tui_pid"] = self.initial_tui.pid
        self.step("ordinary_tui_started", tui_pid=self.initial_tui.pid)
        self.observer = Rpc(self.endpoint)

        def discover_thread() -> bool:
            rows = self.observer.call(
                "thread/list", {"cwd": str(self.workspace), "limit": 20}, timeout=5
            )["data"]
            if len(rows) != 1:
                return False
            self.thread = rows[0]["id"]
            self.report["thread"] = self.thread
            return True

        self.wait_for(self.initial_tui, discover_thread, "owned thread discovery")
        self.wait_for(
            self.initial_tui,
            lambda: self.screen_has_ack(self.initial_tui, INITIAL_MARKER),
            "initial TUI ACK",
        )
        self.wait_for(
            self.initial_tui,
            lambda: self.screen_is_idle(self.initial_tui),
            "initial TUI idle",
        )
        assert self.observer is not None and self.thread is not None
        self.wait_for(
            self.initial_tui,
            lambda: any(
                turn.get("status") == "completed"
                and self.agent_acks([turn], INITIAL_MARKER)
                for turn in self.history(self.observer, self.thread)
            ),
            "initial native turn completed",
        )
        initial_turns = self.history(self.observer, self.thread)
        self.check(
            "initial_thread_history_exact",
            len(self.agent_acks(initial_turns, INITIAL_MARKER)) == 1,
        )
        self.capture("initial", self.initial_tui)
        self.initial_tui.close()
        self.initial_tui = None
        self.step("initial_tui_closed_before_dashboard_open")

        assert self.monitor is None and self.state is not None
        self.monitor = Monitor(self.state, lambda _endpoint: (_ for _ in ()).throw(
            AssertionError("dashboard canary must not dispatch")
        ))
        self.monitor.bind("open-thread", self.thread, self.endpoint, ["dashboard-canary"])
        self.step("dashboard_binding_created", binding="open-thread")

        dashboard_command = [
            self.args.dashboard_python or sys.executable,
            "-m", "codex_monitor", "--state", str(self.state),
            "dashboard", "--interval", ".1", "--color", "never", "--no-animate",
        ]
        dashboard_env = (
            {"PYTHONPATH": ""}
            if self.args.dashboard_python
            else {"PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        )
        self.dashboard = self.terminal_class(
            dashboard_command, width=120, height=24, cwd=self.workspace,
            env=dashboard_env,
        )
        self.report["process_model"]["dashboard_pid"] = self.dashboard.pid
        self.step("dashboard_started", dashboard_pid=self.dashboard.pid)
        self.wait_for(
            self.dashboard,
            lambda: (
                "CONVERSATIONS" in self.dashboard.text()
                and "open-thread" in self.dashboard.text()
                and "▶" in self.dashboard.text()
            ),
            "dashboard selected binding",
        )
        self.check("dashboard_selected_exact_thread", True, thread=self.thread)

        self.trust_sent = False
        self.dashboard.send("\r")
        self.step("dashboard_enter_pressed", key="enter")
        self.wait_for(
            self.dashboard,
            lambda: self.screen_is_idle(self.dashboard)
            and "codex-monitor dashboard" not in self.dashboard.text().lower(),
            "opened ordinary TUI",
        )
        self.check("dashboard_opened_existing_tui", True)

        self.dashboard.send("marker " + FOLLOWUP_MARKER)
        self.wait_for(
            self.dashboard,
            lambda: FOLLOWUP_MARKER in self.dashboard.text(),
            "followup rendered in opened TUI composer",
            timeout=15,
        )
        time.sleep(.2)
        self.dashboard.send("\r")
        self.wait_for(
            self.dashboard,
            lambda: self.screen_has_ack(self.dashboard, FOLLOWUP_MARKER),
            "opened TUI followup ACK",
        )
        self.wait_for(
            self.dashboard,
            lambda: self.screen_is_idle(self.dashboard),
            "opened TUI idle after followup",
        )
        self.wait_for(
            self.dashboard,
            lambda: any(
                turn.get("status") == "completed"
                and self.agent_acks([turn], FOLLOWUP_MARKER)
                for turn in self.history(self.observer, self.thread)
            ),
            "opened TUI followup turn completed",
        )
        final_turns = self.history(self.observer, self.thread)
        self.check(
            "followup_same_thread_history_exact",
            len(self.user_markers(final_turns, FOLLOWUP_MARKER)) == 1
            and len(self.agent_acks(final_turns, FOLLOWUP_MARKER)) == 1,
        )
        self.check(
            "initial_and_followup_share_thread",
            len(self.agent_acks(final_turns, INITIAL_MARKER)) == 1,
            thread=self.thread,
        )
        self.capture("opened-tui", self.dashboard)

        self.dashboard.send("/quit")
        self.wait_for(
            self.dashboard,
            lambda: "/quit" in self.dashboard.text(),
            "quit command rendered in opened TUI",
            timeout=5,
        )
        self.dashboard.send("\r")
        self.step("opened_tui_exit_pressed", command="/quit")
        self.wait_for(
            self.dashboard,
            lambda: (
                "codex-monitor dashboard" in self.dashboard.text().lower()
                and "open-thread" in self.dashboard.text()
                and "▶" in self.dashboard.text()
            ),
            "dashboard returned after TUI exit",
        )
        self.check("dashboard_returned_after_tui_exit", True)
        self.capture("dashboard-returned", self.dashboard)
        self.report["functional_checks_complete"] = True
        self.step("dashboard_open_lifecycle_complete", result="FUNCTIONAL_CHECKS_COMPLETE")

    def cleanup(self) -> None:
        errors: list[str] = []
        processes_gone = True
        for name, terminal in (("dashboard", self.dashboard), ("initial_tui", self.initial_tui)):
            if terminal is None:
                continue
            try:
                self.capture(name + "-cleanup", terminal)
            except Exception as exc:
                errors.append(f"capture_{name}: {type(exc).__name__}: {exc}")
            try:
                terminal.close()
            except Exception as exc:
                errors.append(f"close_{name}: {type(exc).__name__}: {exc}")
            if terminal.returncode is None:
                errors.append(f"{name}_process_still_alive")
                processes_gone = False
        self.dashboard = None
        self.initial_tui = None

        if self.observer is not None:
            try:
                self.observer.close()
            except Exception as exc:
                errors.append(f"close_observer: {type(exc).__name__}: {exc}")
            self.observer = None

        if self.thread and self.endpoint and self.remote_server_class is not None:
            archive_rpc = None
            try:
                archive_rpc = Rpc(self.endpoint)
                archive_rpc.call("thread/archive", {"threadId": self.thread}, timeout=5)
                self.report["checks"]["owned_thread_archived"] = True
                self.step("owned_thread_archived")
            except Exception as exc:
                errors.append(f"archive_owned_thread: {type(exc).__name__}: {exc}")
            finally:
                if archive_rpc is not None:
                    archive_rpc.close()

        if self.owner is not None:
            try:
                self.owner.close()
            except Exception as exc:
                errors.append(f"close_owner: {type(exc).__name__}: {exc}")
            if self.owner.proc.poll() is None:
                errors.append("owner_process_still_alive")
                processes_gone = False
            self.owner = None
        if processes_gone:
            self.report["checks"]["owned_processes_gone"] = True
        if self.tempdir is not None:
            temporary_root = Path(self.tempdir.name)
            try:
                self.tempdir.cleanup()
            except Exception as exc:
                errors.append(f"remove_workspace: {type(exc).__name__}: {exc}")
            if temporary_root.exists():
                errors.append("temporary_root_still_exists")
            else:
                self.report["checks"]["temporary_root_removed"] = True
            self.tempdir = None
        if errors:
            self.report["cleanup_errors"] = errors
            self.report["result"] = "FAIL"

    def finish(self) -> None:
        self.report["finished_at"] = utc_now()
        self.save()
        print(json.dumps(self.report, indent=2, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--dashboard-python",
        help="optional installed Python interpreter used for the dashboard process",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    canary = Canary(args)
    canary.save()
    run_ok = False
    try:
        canary.run()
        run_ok = bool(canary.report.get("functional_checks_complete"))
    except BaseException as exc:
        canary.report["result"] = "FAIL"
        canary.report["error"] = f"{type(exc).__name__}: {exc}"
        canary.step("dashboard_open_lifecycle_failed", error=canary.report["error"])
    finally:
        canary.cleanup()
        if run_ok and not canary.report.get("cleanup_errors") and canary.report["checks"].get("owned_thread_archived"):
            canary.report["result"] = "PASS"
        else:
            canary.report["result"] = "FAIL"
        canary.finish()
    return 0 if canary.report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
