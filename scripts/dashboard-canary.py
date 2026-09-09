#!/usr/bin/env python3
"""Opt-in PTY canary for the read-only terminal dashboard.

This canary creates a disposable monitor state and a loopback receiver.  It
does not contact Codex, create a conversation, or invoke a model.  The live
dashboard is driven through a real PTY so keyboard input, terminal resizing,
receiver loss/recovery, and terminal cleanup are exercised together.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
from typing import Any


try:
    import pyte
except ImportError as exc:  # pragma: no cover - exercised on hosts without the test extra
    pyte = None
    PYTE_ERROR = f"{type(exc).__name__}: {exc}"
else:
    PYTE_ERROR = None


ROOT = Path(__file__).resolve().parents[1]
THREAD = "dashboard-canary-thread"
OTHER_THREAD = "dashboard-canary-other-thread"
ADMIN_TOKEN = "dashboard-canary-admin-token"

sys.path.insert(0, str(ROOT))
from codex_monitor.http import Server  # noqa: E402
from codex_monitor.lock import ProcessLock  # noqa: E402
from codex_monitor.managed import ManagedSupervisor  # noqa: E402
from codex_monitor.monitor import Monitor  # noqa: E402


class RecordingSession:
    """A local sink in case a test accidentally produces a delivery."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def deliver(self, thread: str, client_id: str, text: str) -> dict[str, str]:
        self.calls.append((thread, client_id, text))
        return {"submission_id": "dashboard-canary-sink"}

    def reconcile(self, thread: str, client_id: str) -> dict[str, str] | None:
        return None


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def sqlite_semantics(root: Path) -> dict[str, Any]:
    """Return deterministic table contents for every SQLite file in root."""

    result: dict[str, Any] = {}
    for path in sorted(root.glob("*.sqlite3")):
        uri = "file:" + str(path.resolve()).replace("%", "%25").replace("?", "%3F") + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=.5)
        except sqlite3.Error as exc:
            result[path.name] = {"error": type(exc).__name__}
            continue
        try:
            tables = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            inventory: dict[str, Any] = {"schema": [[name, sql] for name, sql in tables], "rows": {}}
            for name, _ in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                try:
                    rows = db.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
                except sqlite3.Error:
                    rows = db.execute(f"SELECT * FROM {quoted}").fetchall()
                inventory["rows"][name] = [list(map(_json_value, row)) for row in rows]
            result[path.name] = inventory
        finally:
            db.close()
    return result


class Terminal:
    """Own one disposable PTY process group and a pyte terminal buffer."""

    def __init__(self, argv: list[str], *, width: int = 100, height: int = 12,
                 env: dict[str, str] | None = None, cwd: Path | None = None):
        if pyte is None:
            raise RuntimeError("pyte is unavailable")
        self.argv = argv
        self.width = width
        self.height = height
        self.env = env
        self.cwd = cwd
        self.screen = pyte.HistoryScreen(width, height, history=1000)
        self.raw = bytearray()
        self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        self.returncode: int | None = None
        self.closed = False
        if self.pid == 0:
            child_env = os.environ.copy()
            child_env["TERM"] = "xterm-256color"
            child_env.pop("NO_COLOR", None)
            if env:
                child_env.update(env)
            if cwd is not None:
                os.chdir(cwd)
            try:
                os.execvpe(argv[0], argv, child_env)
            except BaseException:
                os._exit(127)
        self.resize(width, height)

    def resize(self, width: int, height: int) -> None:
        self.width = max(20, int(width))
        self.height = max(3, int(height))
        fcntl_ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.height, self.width, 0, 0))
        with suppress(Exception):
            self.screen.resize(self.height, self.width)

    def _poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0
            return self.returncode
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            self.returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.returncode = 128 + os.WTERMSIG(status)
        else:
            self.returncode = 1
        return self.returncode

    def pump(self, seconds: float = .1) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0:
                break
            if select.select([self.fd], [], [], min(.1, remaining))[0]:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if data:
                    self.raw.extend(data)
                    self.stream.feed(data)
                    if b"\x1b[6n" in data:
                        with suppress(OSError):
                            os.write(self.fd, b"\x1b[1;1R")
                    if b"\x1b[c" in data:
                        with suppress(OSError):
                            os.write(self.fd, b"\x1b[?1;2c")
                else:
                    self._poll()
                    break
            self._poll()
            if self.returncode is not None:
                # Drain output already queued by the PTY before returning.
                if not select.select([self.fd], [], [], 0)[0]:
                    break

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def send(self, value: str | bytes) -> None:
        if self.returncode is not None:
            raise RuntimeError("cannot send input to an exited PTY process")
        data = value.encode() if isinstance(value, str) else value
        os.write(self.fd, data)

    def wait_for(self, predicate, timeout: float, label: str) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(.05)
            current = self.text()
            if predicate(current):
                return current
            if self.returncode is not None:
                raise RuntimeError(f"{label}: PTY exited with {self.returncode}")
        raise TimeoutError(f"{label} timed out")

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.pump(.05)
            if self.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    os.killpg(self.pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while self.returncode is None and time.monotonic() < deadline:
                    self.pump(.05)
                if self.returncode is None:
                    with suppress(ProcessLookupError, OSError):
                        os.killpg(self.pid, signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while self.returncode is None and time.monotonic() < deadline:
                        self.pump(.05)
                if self.returncode is None:
                    with suppress(ChildProcessError):
                        os.waitpid(self.pid, 0)
                    self._poll()
        finally:
            with suppress(OSError):
                os.close(self.fd)
            self.closed = True


def fcntl_ioctl(fd: int, operation: int, data: bytes) -> None:
    """Keep the PTY ioctl in one small helper for platforms with fcntl."""

    import fcntl

    fcntl.ioctl(fd, operation, data)


class Canary:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started = time.monotonic()
        self.report: dict[str, Any] = {
            "result": "RUNNING",
            "scope": "disposable dashboard PTY / loopback receiver / no model",
            "checks": {},
            "steps": [],
            "cleanup": {},
        }
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.state: Path | None = None
        self.monitor: Monitor | None = None
        self.session = RecordingSession()
        self.lock: ProcessLock | None = None
        self.server: Server | None = None
        self.supervisor: ManagedSupervisor | None = None
        self.terminals: list[Terminal] = []
        self.db_before: dict[str, Any] | None = None
        self.failure: str | None = None

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

    @staticmethod
    def dashboard_header(text: str) -> bool:
        """Recognize the stable dashboard identity across visual skins."""

        header = "\n".join(text.lower().splitlines()[:3])
        return "codex-monitor" in header and any(
            marker in header for marker in ("live", "snapshot", "last refreshed", "updated")
        )

    @staticmethod
    def receiver_ready(text: str, raw: bytes = b"") -> bool:
        """Check receiver readiness using the rendered state or its ANSI color."""

        header = "\n".join(text.lower().splitlines()[:8])
        if "receiver" in header and "ready" in header and "not ready" not in header:
            return True
        receiver_lines = [line for line in raw.splitlines() if b"Receiver" in line]
        return bool(receiver_lines and b"\x1b[32m" in receiver_lines[-1]) or (
            b"Receiver" in raw and b"\x1b[32m" in raw
        )

    @staticmethod
    def receiver_stopped(text: str, raw: bytes = b"") -> bool:
        """Check an outage using the rendered state or its ANSI color."""

        header = "\n".join(text.lower().splitlines()[:8])
        if "receiver" in header and any(
            marker in header for marker in ("stopped", "offline", "not ready")
        ):
            return True
        receiver_lines = [line for line in raw.splitlines() if b"Receiver" in line]
        return bool(receiver_lines and b"\x1b[31m" in receiver_lines[-1])

    @staticmethod
    def inventory_header(text: str) -> bool:
        """Recognize the conversation inventory without a specific table label."""

        upper = text.upper()
        return "CONVERSATIONS" in upper or "BINDINGS" in upper or "MONITORS" in upper

    @staticmethod
    def selected_row(text: str) -> str | None:
        """Return the rendered selected row so navigation is tested directly."""

        return next((line.strip() for line in text.splitlines() if "▶" in line or "▌" in line), None)

    @staticmethod
    def route_context(text: str, bindings: tuple[str, ...] = ()) -> str:
        """Return the bottom route context used by the conversation-focused view."""

        lines = []
        for line in text.splitlines()[-8:]:
            lower = line.lower()
            if not any(marker in lower for marker in ("route", "binding", "endpoint")):
                continue
            if bindings and not any(binding.lower() in lower for binding in bindings):
                continue
            lines.append(line.strip())
        return "\n".join(lines)

    @staticmethod
    def active_route(text: str, bindings: tuple[str, ...]) -> str | None:
        """Identify the active route when the footer shows one route at a time."""

        context = Canary.route_context(text, bindings)
        matches = [binding for binding in bindings if binding.lower() in context.lower()]
        if len(matches) == 1:
            return matches[0]
        return context or None

    def setup(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-monitor-dashboard-")
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        sample = self.state / "collector-input.txt"
        sample.write_text("dashboard canary baseline\n")
        self.monitor = Monitor(self.state, lambda _endpoint: self.session)
        for name, sources in (
            ("alpha", ["webhook"]),
            ("beta", ["agent"]),
            ("paused", ["operator"]),
        ):
            self.monitor.bind(name, THREAD, "shared-local", sources)
        self.monitor.bind("gamma", OTHER_THREAD, "shared-local", ["other-thread"])
        self.monitor.set_conversation_metadata(OTHER_THREAD, "A", "gamma")
        self.monitor.set_conversation_metadata(THREAD, "B", "alpha")
        self.monitor.enable("paused", False)
        self.monitor.managed_create(THREAD, "collector", str(sample), .1)
        self.supervisor = ManagedSupervisor(self.monitor, poll_interval=.05, sample_timeout=.5)
        self.supervisor.start()
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            status = self.monitor.managed_status(THREAD, "collector")
            if status.get("last_sample") is not None:
                break
            time.sleep(.05)
        else:
            raise TimeoutError("managed collector did not establish a baseline")
        self.supervisor.close()
        self.supervisor = None
        self.check("managed_collector_is_visible", bool(status.get("last_sample")))

        token_path = self.state / "admin.token"
        token_path.write_text(ADMIN_TOKEN + "\n")
        os.chmod(token_path, 0o600)
        (self.state / "config.json").write_text(json.dumps({
            "version": 1, "port": 0, "sources": {}, "limits": {},
        }) + "\n")
        self._acquire_lock()
        self.start_receiver()
        self.db_before = sqlite_semantics(self.state)
        self.step("disposable_state_ready", state=str(self.state))

    def _acquire_lock(self) -> None:
        if self.lock is None:
            self.lock = ProcessLock(self.state / "serve.lock")
            self.lock.__enter__()

    def _release_lock(self) -> None:
        if self.lock is not None:
            self.lock.__exit__(None, None, None)
            self.lock = None

    def start_receiver(self) -> None:
        assert self.monitor is not None
        self.server = Server(self.monitor, {}, ADMIN_TOKEN, port=0).start(dispatch=False)
        port = self.server.http.server_port
        assert self.state is not None
        (self.state / "config.json").write_text(json.dumps({
            "version": 1, "port": port, "sources": {}, "limits": {},
        }) + "\n")

    def stop_receiver(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def json_snapshot(self) -> None:
        assert self.state is not None
        env = os.environ.copy()
        env.update(self.dashboard_env)
        command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            "--interval", str(self.args.interval), "--once", "--json", "--thread", THREAD,
        ]
        completed = subprocess.run(
            command, cwd=self.dashboard_cwd, env=env, capture_output=True, text=True,
            timeout=min(self.args.timeout, 10), check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")[:300]
            raise RuntimeError(f"dashboard JSON command exited {completed.returncode}: {detail}")
        payload = json.loads(completed.stdout)
        connections = payload.get("connections") or []
        bindings = [item.get("name") for row in connections for item in row.get("bindings", [])]
        collectors = [item.get("name") for row in connections for item in row.get("collectors", [])]
        filtered_threads = {row.get("thread") for row in connections}
        self.check("json_snapshot_is_read_only", payload.get("read_only") is True)
        self.check("json_snapshot_filters_thread", payload.get("thread_filter") == THREAD)
        self.check("json_snapshot_contains_bindings", {"alpha", "beta", "paused"} <= set(bindings))
        self.check("json_snapshot_excludes_other_thread", "gamma" not in bindings and filtered_threads <= {THREAD})
        self.check("json_snapshot_contains_collector", "collector" in collectors)
        self.check("json_snapshot_reports_receiver_ready", payload.get("receiver", {}).get("ready") is True)

        text_command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            "--interval", str(self.args.interval), "--once",
        ]
        text_snapshot = subprocess.run(
            text_command, cwd=self.dashboard_cwd, env=env, capture_output=True, text=True,
            timeout=min(self.args.timeout, 10), check=False,
        )
        if text_snapshot.returncode != 0:
            raise RuntimeError("dashboard text snapshot exited " + str(text_snapshot.returncode))
        text_lines = text_snapshot.stdout.splitlines()
        self.check("text_snapshot_contains_multiple_bindings",
                   all(name in text_snapshot.stdout for name in ("alpha", "beta", "paused", "gamma")))
        self.check("text_snapshot_contains_collector", "collector" in text_snapshot.stdout.lower())
        self.check("text_snapshot_contains_conversation_inventory", self.inventory_header(text_snapshot.stdout))
        snapshot_lower = text_snapshot.stdout.lower()
        self.check(
            "text_snapshot_labels_configuration_states",
            ("on" in snapshot_lower or "enabled" in snapshot_lower)
            and ("off" in snapshot_lower or "paused" in snapshot_lower or "disabled" in snapshot_lower)
            and ("unknown" in snapshot_lower or "unavailable" in snapshot_lower),
        )
        self.check("text_snapshot_has_no_terminal_controls", "\x1b" not in text_snapshot.stdout)
        self.check("text_snapshot_lines_fit_default_width", all(len(line) <= 100 for line in text_lines))
        self.check("text_snapshot_has_persistent_header", bool(text_lines[:3]) and self.dashboard_header(text_snapshot.stdout))

        no_color_command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            "--interval", str(self.args.interval), "--once", "--color", "never",
        ]
        no_color_snapshot = subprocess.run(
            no_color_command, cwd=self.dashboard_cwd, env=env, capture_output=True, text=True,
            timeout=min(self.args.timeout, 10), check=False,
        )
        self.check("no_color_snapshot_is_plain", no_color_snapshot.returncode == 0 and "\x1b" not in no_color_snapshot.stdout)

        color_snapshot = subprocess.run(
            [*text_command, "--color", "always"], cwd=self.dashboard_cwd, env=env,
            capture_output=True, text=True, timeout=min(self.args.timeout, 10), check=False,
        )
        self.check(
            "color_snapshot_preserves_status_palette",
            color_snapshot.returncode == 0
            and all(code in color_snapshot.stdout for code in ("\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[90m")),
        )

        all_json_command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            "--interval", str(self.args.interval), "--once", "--json",
        ]
        all_snapshot = subprocess.run(
            all_json_command, cwd=self.dashboard_cwd, env=env, capture_output=True, text=True,
            timeout=min(self.args.timeout, 10), check=False,
        )
        if all_snapshot.returncode != 0:
            raise RuntimeError("unfiltered dashboard JSON snapshot exited " + str(all_snapshot.returncode))
        all_payload = json.loads(all_snapshot.stdout)
        all_bindings = [item.get("name") for row in all_payload.get("connections", [])
                        for item in row.get("bindings", [])]
        self.check("unfiltered_json_contains_other_thread", "gamma" in all_bindings and
                   all_payload.get("thread_filter") is None)

        missing = self.state.parent / "missing-state"
        if missing.exists():
            raise AssertionError("missing-state fixture unexpectedly exists before CLI read")
        missing_command = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(missing), "dashboard",
            "--interval", str(self.args.interval), "--once", "--json", "--thread", THREAD,
        ]
        missing_snapshot = subprocess.run(
            missing_command, cwd=self.dashboard_cwd, env=env, capture_output=True, text=True,
            timeout=min(self.args.timeout, 10), check=False,
        )
        missing_payload = json.loads(missing_snapshot.stdout)
        self.check("missing_state_snapshot_fails_read_only", missing_snapshot.returncode == 2 and
                   missing_payload.get("ok") is False and missing_payload.get("read_only") is True)
        self.check("missing_state_remains_missing", not missing.exists())
        self.step("json_snapshot_verified")

    def dashboard_command(self, thread: str | None = THREAD) -> list[str]:
        assert self.state is not None
        dashboard_args = [
            self.dashboard_python, "-m", "codex_monitor", "--state", str(self.state), "dashboard",
            # Keep the live poll comfortably slower than the animation check
            # below so both frames can be observed against one snapshot.
            "--interval", str(max(2.0, self.args.interval)),
        ]
        if thread is not None:
            dashboard_args.extend(["--thread", thread])
        dashboard = shlex.join(dashboard_args)
        probe = shlex.join([self.dashboard_python, "-c", "import termios; print(termios.tcgetattr(0))"])
        shell = (
            "before=$(" + probe + " | cksum | tr -d '[:space:]'); " + dashboard + "; rc=$?; "
            "after=$(" + probe + " | cksum | tr -d '[:space:]'); "
            "printf '\\nCANARY_TERMIO:%s:%s:%s\\n' \"$before\" \"$after\" \"$rc\"; exit \"$rc\""
        )
        return ["/bin/sh", "-c", shell]

    def termios_check(self, text: str, label: str) -> None:
        match = re.search(r"CANARY_TERMIO:([^:\s]+):([^:\s]+):(\d+)", text)
        if not match:
            raise AssertionError(f"{label} did not report termios state")
        before, after, code = match.groups()
        if before != after or code != "0":
            raise AssertionError(f"{label}_termios_restored ({before} != {after}, exit {code})")
        self.check(f"{label}_termios_restored", True, before=before, after=after, code=code)

    def live_q(self) -> None:
        terminal = Terminal(self.dashboard_command(thread=None), width=100, height=12,
                            env=self.dashboard_env, cwd=self.dashboard_cwd)
        self.terminals.append(terminal)
        ready_timeout = max(4.0, self.args.interval * 2 + 2)
        initial = terminal.wait_for(
            lambda text: self.receiver_ready(text, bytes(terminal.raw)) and self.dashboard_header(text),
            ready_timeout, "dashboard initial render",
        )
        terminal.pump(.1)
        initial = terminal.text()
        self.check("live_dashboard_reports_receiver_ready", self.receiver_ready(initial, bytes(terminal.raw)))
        route_bindings = ("alpha", "beta", "paused", "collector", "gamma")
        if self.route_context(initial):
            self.check("live_dashboard_renders_conversation_inventory", self.inventory_header(initial))
            self.check("live_dashboard_renders_route_context", bool(self.route_context(initial)))
            before_navigation = terminal.text()
            terminal.send("j")
            thread_view = terminal.wait_for(
                lambda text: bool(self.route_context(text))
                and any(name in text.lower() for name in ("alpha", "beta", "paused", "collector")),
                2, "dashboard conversation navigation",
            )
            route_before = self.active_route(thread_view, route_bindings)
            context_before = self.route_context(thread_view, route_bindings)
            terminal.send("\t")
            routed = terminal.wait_for(
                lambda text: bool(self.route_context(text, route_bindings))
                and self.route_context(text, route_bindings) != context_before,
                2, "dashboard route tab navigation",
            )
            route_after = self.active_route(routed, route_bindings)
            self.check("route_tab_cycles_binding", route_before != route_after or context_before != self.route_context(routed, route_bindings))
            self.check("route_context_shows_binding", route_after is not None)
            terminal.send("\x1b[H")
            at_home = terminal.wait_for(
                lambda text: "gamma" in text.lower() and bool(self.route_context(text)),
                2, "dashboard other-thread row",
            )
            self.check("live_unfiltered_dashboard_reaches_other_thread", "gamma" in at_home.lower())
            self.check(
                "row_navigation_changes_selection",
                self.selected_row(before_navigation) is not None
                and self.selected_row(thread_view) is not None
                and self.selected_row(before_navigation) != self.selected_row(thread_view),
            )
        else:
            self.check("live_dashboard_renders_multiple_bindings", all(name in initial for name in ("alpha", "beta", "paused")))
            self.check(
                "live_dashboard_renders_paused_binding",
                "paused" in initial.lower(),
            )
            before_navigation = terminal.text()
            terminal.send("j")
            terminal.pump(.2)
            terminal.send("\x1b[B")
            terminal.pump(.2)
            collector_view = terminal.wait_for(lambda text: "collector" in text.lower(), 2, "dashboard collector row")
            self.check("compact_table_shows_collector", "collector" in collector_view.lower())
            terminal.send("\x1b[F")
            at_end = terminal.wait_for(lambda text: "gamma" in text, 2, "dashboard row navigation")
            self.check("row_navigation_reaches_last_conversation", "gamma" in at_end)
            terminal.send("\x1b[H")
            at_home = terminal.wait_for(lambda text: "gamma" in text, 2, "dashboard other-thread row")
            self.check("live_unfiltered_dashboard_reaches_other_thread", "gamma" in at_home)
            self.check(
                "row_navigation_changes_selection",
                self.selected_row(before_navigation) is not None
                and self.selected_row(at_end) is not None
                and self.selected_row(before_navigation) != self.selected_row(at_end),
            )
            terminal.wait_for(lambda text: self.inventory_header(text), 2, "dashboard home navigation")

        sample_start = len(terminal.raw)
        terminal.pump(.9)
        raw = bytes(terminal.raw[sample_start:])
        pulse_text = bytes(terminal.raw).decode("utf-8", errors="replace")
        title_pulses = set(re.findall(r"codex-monitor[^\r\n]*([●○])[^\r\n]*Live", pulse_text))
        self.check(
            "live_indicator_animates_independently",
            {"●", "○"} <= title_pulses,
        )
        self.check(
            "live_frames_render_current_header",
            pulse_text.count("codex-monitor") >= 2 and "Live" in pulse_text,
        )
        rendered_ages = set(re.findall(r"Updated ([^\r\n\x1b]+?) ago", pulse_text))
        self.check("snapshot_age_is_rendered", bool(rendered_ages))
        if len(rendered_ages) < 2:
            terminal.pump(1.2)
            rendered_ages = set(re.findall(r"Updated ([^\r\n\x1b]+?) ago", bytes(terminal.raw).decode("utf-8", errors="replace")))
        self.check("snapshot_age_changes_between_frames", len(rendered_ages) >= 2)
        self.check("live_color_attributes_are_present", b"\x1b[36m" in raw and b"\x1b[32m" in raw)
        self.check("live_renderer_avoids_full_screen_clear", b"\x1b[2J" not in raw)

        terminal.resize(42, 8)
        narrow = terminal.wait_for(lambda text: self.dashboard_header(text), ready_timeout, "dashboard narrow resize")
        narrow_lines = [line.rstrip() for line in narrow.splitlines() if line]
        self.check("narrow_resize_keeps_lines_bounded", all(len(line) <= 42 for line in narrow_lines))
        self.check("narrow_resize_keeps_dashboard_visible", self.dashboard_header(narrow))
        terminal.resize(100, 12)

        self.stop_receiver()
        self._release_lock()
        stopped = terminal.wait_for(lambda text: self.receiver_stopped(text, bytes(terminal.raw)), ready_timeout, "dashboard receiver outage")
        self.check("receiver_outage_is_visible", self.receiver_stopped(stopped, bytes(terminal.raw)))
        self._acquire_lock()
        self.start_receiver()
        recovered = terminal.wait_for(
            lambda text: self.receiver_ready(text, bytes(terminal.raw)),
            ready_timeout, "dashboard receiver recovery",
        )
        self.check("receiver_recovery_is_visible", self.receiver_ready(recovered, bytes(terminal.raw)))

        terminal.send("q")
        terminal.wait_for(lambda text: "CANARY_TERMIO:" in text, 3, "dashboard q cleanup")
        terminal.close()
        self.check("q_process_cleaned_up", terminal.returncode == 0 and terminal.closed)
        self.termios_check(terminal.raw.decode("utf-8", errors="replace"), "q")
        self.step("live_dashboard_q_verified")

    def live_ctrl_c(self) -> None:
        terminal = Terminal(self.dashboard_command(thread=THREAD), width=100, height=12,
                            env=self.dashboard_env, cwd=self.dashboard_cwd)
        self.terminals.append(terminal)
        terminal.wait_for(
            lambda text: self.dashboard_header(text),
            max(4.0, self.args.interval * 2 + 2), "dashboard Ctrl-C initial render",
        )
        terminal.send(b"\x03")
        terminal.wait_for(lambda text: "CANARY_TERMIO:" in text, 3, "dashboard Ctrl-C cleanup")
        terminal.close()
        self.check("ctrl_c_process_cleaned_up", terminal.returncode == 0)
        self.termios_check(terminal.raw.decode("utf-8", errors="replace"), "ctrl_c")
        self.step("live_dashboard_ctrl_c_verified")

    def run(self) -> None:
        self.setup()
        self.json_snapshot()
        self.live_q()
        self.live_ctrl_c()
        assert self.state is not None and self.db_before is not None
        self.check("dashboard_does_not_mutate_database", sqlite_semantics(self.state) == self.db_before)
        self.check("no_model_session_calls", not self.session.calls)

    def cleanup(self) -> None:
        errors: list[str] = []
        for terminal in reversed(self.terminals):
            try:
                terminal.close()
            except Exception as exc:
                errors.append("terminal: " + type(exc).__name__ + ": " + str(exc))
        if self.supervisor is not None:
            try:
                self.supervisor.close()
            except Exception as exc:
                errors.append("managed supervisor: " + type(exc).__name__ + ": " + str(exc))
            self.supervisor = None
        try:
            self.stop_receiver()
        except Exception as exc:
            errors.append("receiver: " + type(exc).__name__ + ": " + str(exc))
        try:
            self._release_lock()
        except Exception as exc:
            errors.append("receiver lock: " + type(exc).__name__ + ": " + str(exc))
        remaining = [terminal.pid for terminal in self.terminals if terminal.returncode is None]
        self.report["cleanup"] = {"errors": errors, "remaining_owned_ptys": remaining}
        if errors or remaining:
            self.failure = self.failure or "cleanup did not complete"
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run", required=True, action="store_true", help="explicitly opt in to the dashboard canary")
    value.add_argument("--report", required=True, type=Path, help="JSON report path outside the repository")
    value.add_argument("--timeout", type=float, default=30.0, help="per-condition timeout in seconds")
    value.add_argument("--interval", type=float, default=2.0, help="dashboard refresh interval in seconds")
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
    if args.timeout <= 0 or args.interval <= 0:
        parser().error("timeout and interval must be positive")
    if args.dashboard_python is not None:
        # Preserve a venv's launcher symlink: resolving it can select the
        # base interpreter and lose the wheel's site-packages directory.
        args.dashboard_python = args.dashboard_python.expanduser().absolute()
        if not args.dashboard_python.is_file() or not os.access(args.dashboard_python, os.X_OK):
            parser().error(f"dashboard Python executable does not exist: {args.dashboard_python}")
    if pyte is None:
        return incomplete(args.report, "pyte is required for PTY verification: " + str(PYTE_ERROR))
    if os.name != "posix":
        return incomplete(args.report, "PTY canary requires a POSIX host")

    canary = Canary(args)
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
