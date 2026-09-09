#!/usr/bin/env python3
"""Opt-in PTY canary for dashboard route controls and conversation metadata.

The canary creates a disposable local monitor state, drives the interactive
dashboard through a real PTY, and verifies that each keyboard action addresses
the exact selected route.  It uses no model, Codex conversation, or external
service.  The PTY implementation is loaded from ``dashboard-canary.py`` so
both canaries exercise the same terminal harness.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_dashboard_canary():
    path = ROOT / "scripts" / "dashboard-canary.py"
    spec = importlib.util.spec_from_file_location("dashboard_canary_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load PTY helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard_canary = _load_dashboard_canary()
Terminal = dashboard_canary.Terminal

from codex_monitor.http import Server  # noqa: E402
from codex_monitor.lock import ProcessLock  # noqa: E402
from codex_monitor.monitor import Monitor  # noqa: E402


THREAD = "dashboard-controls-thread"
OTHER_THREAD = "dashboard-controls-other"
ADMIN_TOKEN = "dashboard-controls-admin-token"
PRIMARY = "primary-route"
ALTERNATE = "alternate-route"
OTHER = "other-route"


def _read_state(root: Path) -> dict[str, Any]:
    """Read only the durable facts that controls are required to preserve."""

    db = sqlite3.connect(root / "monitor.sqlite3")
    try:
        binding_columns = {row[1] for row in db.execute("PRAGMA table_info(bindings)")}
        removed = ",removed" if "removed" in binding_columns else ""
        bindings = {}
        for row in db.execute(
            "SELECT name,thread,endpoint,enabled" + removed + " FROM bindings ORDER BY name"
        ):
            values = {
                "name": row[0], "thread": row[1], "endpoint": row[2], "enabled": row[3],
            }
            values["removed"] = row[4] if removed else 0
            bindings[row[0]] = values
        events = [
            {"binding": row[0], "id": row[1], "event_id": row[2]}
            for row in db.execute("SELECT binding,id,event_id FROM events ORDER BY seq")
        ]
        metadata = [tuple(row) for row in db.execute(
            "SELECT thread,project,display_name FROM conversation_metadata ORDER BY thread"
        )]
        return {"bindings": bindings, "events": events, "metadata": metadata}
    finally:
        db.close()


class ControlsCanary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report: dict[str, Any] = {
            "result": "RUNNING",
            "scope": "disposable dashboard controls PTY / no model / no external service",
            "checks": {},
            "steps": [],
            "cleanup": {},
        }
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.state: Path | None = None
        self.monitor: Monitor | None = None
        self.server: Server | None = None
        self.lock: ProcessLock | None = None
        self.terminal: Any | None = None
        self.receipt: str | None = None
        self.failure: str | None = None

    def save(self) -> None:
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        temporary = self.args.report.with_suffix(self.args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.args.report)

    def step(self, name: str, **details: Any) -> None:
        self.report["steps"].append({
            "name": name,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            **details,
        })
        self.save()

    def check(self, name: str, condition: bool, **details: Any) -> None:
        if not condition:
            raise AssertionError(name)
        self.report["checks"][name] = True
        if details:
            self.report.setdefault("check_details", {})[name] = details

    def setup(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-monitor-dashboard-controls-")
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.monitor = Monitor(self.state, lambda _endpoint: None)
        self.monitor.bind(PRIMARY, THREAD, "ws://127.0.0.1:8765", ["primary"])
        self.monitor.bind(ALTERNATE, THREAD, "shared-local", ["alternate"])
        self.monitor.bind(OTHER, OTHER_THREAD, "shared-local", ["other"])
        self.monitor.set_conversation_metadata(THREAD, "project-alpha", "Inbox")
        self.monitor.set_conversation_metadata(OTHER_THREAD, "project-beta", "Inbox")
        receipt = self.monitor.ingest(ALTERNATE, {
            "id": "dashboard-controls-event",
            "source": "alternate",
            "type": "dashboard.controls.canary",
            "data": {"marker": "preserve-receipt"},
        })
        self.receipt = receipt["delivery_id"]

        token_path = self.state / "admin.token"
        token_path.write_text(ADMIN_TOKEN + "\n")
        os.chmod(token_path, 0o600)
        self.lock = ProcessLock(self.state / "serve.lock")
        self.lock.__enter__()
        self.server = Server(self.monitor, {}, ADMIN_TOKEN, port=0).start(dispatch=False)
        port = self.server.http.server_port
        (self.state / "config.json").write_text(json.dumps({
            "version": 1, "port": port, "sources": {}, "limits": {},
        }) + "\n")
        self.step("disposable_controls_state_ready", state=str(self.state))

    @property
    def dashboard_python(self) -> str:
        return str(self.args.dashboard_python or sys.executable)

    @property
    def dashboard_cwd(self) -> Path:
        if self.args.dashboard_python:
            assert self.state is not None
            return self.state
        return ROOT

    @property
    def dashboard_env(self) -> dict[str, str]:
        if self.args.dashboard_python:
            return {"PYTHONPATH": ""}
        return {"PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}

    def start_dashboard(self) -> None:
        assert self.state is not None
        command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            "--interval", "0.2",
        ]
        self.terminal = Terminal(command, width=100, height=24, env=self.dashboard_env, cwd=self.dashboard_cwd)

    def wait_for(self, predicate, label: str) -> str:
        assert self.terminal is not None
        return self.terminal.wait_for(predicate, max(4.0, self.args.timeout), label)

    def wait_notice(self, text: str) -> str:
        return self.wait_for(lambda current: text in current, f"dashboard notice {text}")

    def interact(self) -> None:
        self.start_dashboard()
        assert self.terminal is not None
        initial = self.wait_for(
            lambda text: dashboard_canary.Canary.dashboard_header(text)
            and "Route 2/2" in text
            and PRIMARY in text,
            "initial selected route",
        )
        self.check("initial_route_context_visible", "Route 2/2" in initial and PRIMARY in initial)
        self.check("same_display_name_projects_visible", initial.count("Inbox") >= 2)
        self.check("project_metadata_rows_visible", "project-alpha" in initial and "project-beta" in initial)

        self.terminal.send("\t")
        alternate = self.wait_for(
            lambda text: "Route 1/2" in text and ALTERNATE in text,
            "alternate route selection",
        )
        self.check("tab_selects_alternate_route", "Route 1/2" in alternate and ALTERNATE in alternate)
        self.step("alternate_route_selected")

        self.terminal.send("p")
        self.wait_notice("MONITOR: Stopped route " + ALTERNATE)
        paused = _read_state(self.state)
        self.check("pause_targets_alternate_route", paused["bindings"][ALTERNATE]["enabled"] == 0)
        self.check("pause_preserves_primary_route", paused["bindings"][PRIMARY]["enabled"] == 1)
        self.check("pause_preserves_other_conversation", OTHER in paused["bindings"])

        self.terminal.send("r")
        self.wait_notice("MONITOR: Resumed route " + ALTERNATE)
        resumed = _read_state(self.state)
        self.check("resume_targets_alternate_route", resumed["bindings"][ALTERNATE]["enabled"] == 1)
        self.check("resume_preserves_primary_route", resumed["bindings"][PRIMARY]["enabled"] == 1)
        self.step("pause_resume_verified")

        before_cancel = _read_state(self.state)
        self.terminal.send("x")
        confirmation = self.wait_for(
            lambda text: "DELETE:" in text and ALTERNATE in text and "y confirm" in text,
            "delete confirmation",
        )
        self.check("delete_confirmation_names_selected_route", ALTERNATE in confirmation)
        self.terminal.send("n")
        cancelled = self.wait_notice("MONITOR: Deletion cancelled")
        cancelled_state = _read_state(self.state)
        self.check("delete_cancel_keeps_state", cancelled_state == before_cancel)
        self.check("delete_cancel_keeps_selection", "Route 1/2" in cancelled and ALTERNATE in cancelled)

        self.terminal.send("x")
        self.wait_for(
            lambda text: "DELETE:" in text and ALTERNATE in text,
            "second delete confirmation",
        )
        self.terminal.send("\t")
        tab_cancelled = self.wait_notice("MONITOR: Deletion cancelled")
        tab_cancelled_state = _read_state(self.state)
        self.check("navigation_cancels_delete", tab_cancelled_state == before_cancel)
        self.check("navigation_cancel_keeps_selection", "Route 1/2" in tab_cancelled and ALTERNATE in tab_cancelled)

        self.terminal.send("x")
        self.wait_for(
            lambda text: "DELETE:" in text and ALTERNATE in text,
            "final delete confirmation",
        )
        self.terminal.send("y")
        removed_notice = self.wait_notice("MONITOR: Removed route " + ALTERNATE)
        self.check("delete_notice_names_selected_route", ALTERNATE in removed_notice)
        self.step("delete_confirmation_and_cancellation_verified")

    def verify_persistence(self) -> None:
        assert self.state is not None
        state = _read_state(self.state)
        alternate = state["bindings"].get(ALTERNATE)
        self.check("deleted_route_is_retired", alternate is not None and alternate["removed"] == 1)
        self.check("deleted_route_is_disabled", alternate is not None and alternate["enabled"] == 0)
        primary = state["bindings"].get(PRIMARY)
        other = state["bindings"].get(OTHER)
        self.check(
            "other_route_is_preserved",
            primary is not None and primary["removed"] == 0 and primary["enabled"] == 1,
        )
        self.check(
            "other_conversation_is_preserved",
            other is not None and other["removed"] == 0 and other["enabled"] == 1,
        )
        receipts = [row for row in state["events"] if row["binding"] == ALTERNATE]
        self.check("deleted_route_receipt_is_preserved", len(receipts) == 1 and receipts[0]["id"] == self.receipt)
        self.check("metadata_keeps_same_display_name_projects", set(state["metadata"]) == {
            (THREAD, "project-alpha", "Inbox"),
            (OTHER_THREAD, "project-beta", "Inbox"),
        })
        self.step("persisted_route_and_receipt_state_verified")

    def quit_dashboard(self) -> None:
        if self.terminal is None or self.terminal.returncode is not None:
            return
        self.terminal.send("q")
        self.wait_for(lambda _text: self.terminal is not None and self.terminal.returncode == 0, "dashboard q cleanup")
        self.check("dashboard_process_exited_cleanly", self.terminal.returncode == 0)

    def run(self) -> None:
        self.setup()
        self.interact()
        self.verify_persistence()
        self.quit_dashboard()

    def cleanup(self) -> None:
        errors: list[str] = []
        if self.terminal is not None:
            try:
                self.terminal.close()
            except Exception as exc:
                errors.append("terminal: " + type(exc).__name__ + ": " + str(exc))
        if self.server is not None:
            try:
                self.server.close()
            except Exception as exc:
                errors.append("receiver: " + type(exc).__name__ + ": " + str(exc))
            self.server = None
        if self.lock is not None:
            try:
                self.lock.__exit__(None, None, None)
            except Exception as exc:
                errors.append("receiver lock: " + type(exc).__name__ + ": " + str(exc))
            self.lock = None
        remaining = []
        if self.terminal is not None and self.terminal.returncode is None:
            remaining.append(self.terminal.pid)
        self.report["cleanup"] = {"errors": errors, "remaining_owned_ptys": remaining}
        if errors or remaining:
            self.failure = self.failure or "cleanup did not complete"
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run", required=True, action="store_true", help="explicitly opt in to the controls canary")
    value.add_argument("--report", required=True, type=Path, help="JSON report path outside the repository")
    value.add_argument("--timeout", type=float, default=30.0, help="per-condition timeout in seconds")
    value.add_argument(
        "--dashboard-python", type=Path,
        help="optional absolute Python executable for an installed dashboard wheel",
    )
    return value


def incomplete(report_path: Path, reason: str) -> int:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"result": "INCOMPLETE", "reason": reason, "checks": {}, "cleanup": {}}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout <= 0:
        parser().error("timeout must be positive")
    if args.dashboard_python is not None:
        # Preserve a venv's launcher symlink: resolving it can select the
        # base interpreter and lose the wheel's site-packages directory.
        args.dashboard_python = args.dashboard_python.expanduser().absolute()
        if not args.dashboard_python.is_file() or not os.access(args.dashboard_python, os.X_OK):
            parser().error(f"dashboard Python executable does not exist: {args.dashboard_python}")
    if getattr(dashboard_canary, "pyte", None) is None:
        return incomplete(args.report, "pyte is required for PTY verification: " + str(dashboard_canary.PYTE_ERROR))
    if os.name != "posix":
        return incomplete(args.report, "PTY canary requires a POSIX host")

    canary = ControlsCanary(args)
    try:
        canary.run()
    except Exception as exc:
        canary.failure = f"{type(exc).__name__}: {exc}"
        canary.report["error"] = canary.failure
    finally:
        canary.cleanup()
        canary.report["result"] = "PASS" if canary.failure is None else "FAIL"
        canary.save()
        print(json.dumps({
            "result": canary.report["result"],
            "report": str(args.report),
            "error": canary.report.get("error"),
        }, ensure_ascii=False), flush=True)
    return 0 if canary.report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
