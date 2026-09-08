import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import uuid

from .errors import IngressError
from .http import Server
from .lock import ProcessLock, process_alive
from .monitor import Monitor, NAME
from .monitor import MANAGED_SOURCE
from .managed import ManagedSupervisor
from .session import SessionPool, Rpc, AppServerSession, SharedLocalSession, server_token
from .service import ServiceManager
from .watch import ChangeWatcher, atomic_json, file_sample
from .replies import ReplyStore
from .sessions import overview, display


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def secret(path):
    with Path(path).open("x") as stream:
        os.chmod(path, 0o600)
        stream.write(secrets.token_urlsafe(32) + "\n")


def send(url, binding, envelope, token):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(url.rstrip("/") + "/v1/events/" + binding,
        data=json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode(),
        headers={"Authorization": "Bearer " + token.strip(), "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try: detail = json.load(exc).get("error", "request rejected")
            except (ValueError, AttributeError): detail = "request rejected (redirects are not followed)"
        raise IngressError(detail, exc.code) from exc


def monitor_thread(value):
    environment = os.environ.get("CODEX_THREAD_ID")
    if value and environment and value != environment:
        raise ValueError("--thread does not match CODEX_THREAD_ID")
    thread = value or environment
    if not thread:
        raise ValueError("monitor commands require --thread or CODEX_THREAD_ID; no implicit latest thread is used")
    if not NAME.fullmatch(thread):
        raise ValueError("invalid thread identifier")
    return thread


def parser():
    p = argparse.ArgumentParser(description="Wake the same interactive Codex conversation only for real events.")
    try:
        version = importlib.metadata.version("codex-monitor")
    except importlib.metadata.PackageNotFoundError:
        version = "development (not installed)"
    p.add_argument("--version", action="version", version="codex-monitor " + version)
    default = os.environ.get("CODEX_MONITOR_HOME", str(Path.home() / ".local/state/codex-monitor"))
    p.add_argument("--state", default=default, help="private monitor state directory")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init"); init.add_argument("--port", type=int, default=8766)
    source = commands.add_parser("source"); source.add_argument("name")
    bind = commands.add_parser("bind", aliases=["attach"])
    bind.add_argument("name"); bind.add_argument("--thread", required=True)
    bind.add_argument("--source", action="append", required=True)
    bind.add_argument("--endpoint", default="shared-local")
    commands.add_parser("serve")
    commands.add_parser("status")
    sessions = commands.add_parser("sessions", help="show attached conversations and receiver state without waking Codex")
    sessions.add_argument("name", nargs="?")
    sessions.add_argument("--json", action="store_true")
    reply = commands.add_parser("reply", help="explicitly write a reply for the original event source to retrieve")
    reply.add_argument("delivery_id")
    reply.add_argument("--id", required=True, help="stable reply id; reuse on retry")
    reply.add_argument("--message", required=True, help="reply text, or - for stdin")
    get = commands.add_parser("event"); get.add_argument("id")
    inspect = commands.add_parser("inspect", help="compare local delivery state with read-only native queue/history evidence")
    inspect.add_argument("id")
    for action in ("enable", "disable", "pause", "unpause"):
        commands.add_parser(action).add_argument("binding")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("id"); resolve.add_argument("--as", choices=["accept", "replay", "discard"], dest="action", required=True)
    resolve.add_argument("--reason", required=True)
    emit = commands.add_parser("send")
    emit.add_argument("--to", required=True); emit.add_argument("--source", required=True)
    emit.add_argument("--type", default="agent.message"); emit.add_argument("--id", default=None)
    emit.add_argument("--data", default="{}", help="JSON data, or '-' to read a JSON value from stdin")
    emit.add_argument("--trace"); emit.add_argument("--hops", type=int, default=0)
    emit.add_argument("--url"); emit.add_argument("--token-env")
    watch = commands.add_parser("watch-file")
    watch.add_argument("file"); watch.add_argument("--to", required=True); watch.add_argument("--source", required=True)
    watch.add_argument("--interval", type=float, default=2); watch.add_argument("--url")
    managed = commands.add_parser("monitor", help="manage a file monitor for one explicit conversation")
    managed_commands = managed.add_subparsers(dest="monitor_action", required=True)
    create = managed_commands.add_parser("create")
    create.add_argument("name"); create.add_argument("--file", required=True); create.add_argument("--thread")
    create.add_argument("--interval", type=float, default=2); create.add_argument("--endpoint", default="shared-local")
    listing = managed_commands.add_parser("list"); listing.add_argument("--thread")
    for action in ("status", "pause", "resume", "remove"):
        command = managed_commands.add_parser(action)
        command.add_argument("name"); command.add_argument("--thread")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--endpoint", default="shared-local"); doctor.add_argument("--thread")
    doctor.add_argument("--surface", choices=["cli", "desktop"], default="cli")
    host = commands.add_parser("host"); host.add_argument("--port", type=int, default=8765)
    connect = commands.add_parser("connect")
    connect.add_argument("--endpoint"); connect.add_argument("--cwd", default=os.getcwd())
    service = commands.add_parser("service", help="manage the macOS launchd receiver")
    service.add_argument("action", choices=["install", "start", "stop", "restart", "status", "uninstall"])
    service.add_argument("--codex-home", help="Codex storage home (defaults to CODEX_HOME or ~/.codex)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(args.state).expanduser().resolve()
    config_path = root / "config.json"
    pool = SessionPool()
    try:
        if args.command == "init":
            if config_path.exists():
                raise ValueError("already initialized; existing credentials were preserved")
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            secret(root / "admin.token")
            atomic_json(config_path, {"version": 1, "port": args.port, "sources": {}, "limits": {}})
            output({"state": str(root), "next": "register a source and bind a thread, or create a managed monitor with an explicit --thread"})
            return 0
        if args.command == "doctor":
            rpc = None
            try:
                rpc = Rpc(args.endpoint, token=server_token())
                loaded = rpc.call("thread/loaded/list", {})["data"]
                if args.thread:
                    adapter = SharedLocalSession if args.endpoint == "shared-local" else AppServerSession
                    adapter(rpc).check_target(args.thread)
                    rpc.call("thread/queue/list", {"threadId": args.thread})
                output({"ready": bool(args.thread), "endpoint": args.endpoint, "requested_surface": args.surface,
                        "level": ("shared-queue-ready" if args.endpoint == "shared-local" else "protocol-ready") if args.thread else "endpoint-only", "client_ui_verified": False,
                        "loaded_thread_ids": loaded, "note": "The selected local client must use the same Codex storage; its own server consumes this queue." if args.endpoint == "shared-local" else "The selected client must be using this exact App Server and thread."})
                return 0 if args.thread else 2
            except Exception as exc:
                output({"ready": False, "requested_surface": args.surface, "endpoint": args.endpoint,
                        "reason": str(exc), "fallback_used": False})
                return 2
            finally:
                if rpc: rpc.close()
        if args.command in ("host", "connect"):
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            endpoint = "ws://127.0.0.1:8765"
            if args.command == "host":
                if not 1 <= args.port <= 65535:
                    raise ValueError("port must be between 1 and 65535")
                endpoint = f"ws://127.0.0.1:{args.port}"
                os.execvp("codex", ["codex", "app-server", "--listen", endpoint])
            os.execvp("codex", ["codex", "--remote", args.endpoint or endpoint, "-C", args.cwd])
        config = json.loads(config_path.read_text())
        if config.get("version") != 1:
            raise ValueError("unsupported config version")
        if args.command == "service":
            needs_runtime = args.action in ("install", "start", "restart")
            manager = ServiceManager.from_state(
                root,
                codex_home=args.codex_home,
                require_installed=needs_runtime,
            )
            output(getattr(manager, args.action)())
            return 0
        if args.command == "source":
            if not NAME.fullmatch(args.name) or "/" in args.name:
                raise ValueError("invalid source name")
            if args.name == MANAGED_SOURCE:
                raise ValueError("reserved managed source cannot be registered externally")
            with ProcessLock(root / "config.lock"):
                config = json.loads(config_path.read_text())
                if args.name in config["sources"]:
                    raise ValueError("source already registered")
                token_path = root / ("source-" + args.name + ".token")
                secret(token_path)
                config["sources"][args.name] = {"token_file": str(token_path)}
                atomic_json(config_path, config)
            output({"source": args.name, "token_file": str(token_path), "note": "restart serve to load the new source"})
            return 0
        monitor = Monitor(root, pool, **config.get("limits", {}))
        if args.command == "monitor":
            thread = monitor_thread(args.thread)
            if args.monitor_action == "create":
                path = os.path.abspath(os.path.expanduser(args.file))
                value = monitor.managed_create(thread, args.name, path, args.interval, args.endpoint)
            elif args.monitor_action == "list":
                value = monitor.managed_status(thread)
            elif args.monitor_action == "status":
                value = monitor.managed_status(thread, args.name)
            elif args.monitor_action == "pause":
                value = monitor.managed_set_enabled(thread, args.name, False)
                value["operation_note"] = "Pause atomically disables new managed intake and dispatch; an in-flight dispatch may finish, and already accepted native input is unchanged."
            elif args.monitor_action == "resume":
                value = monitor.managed_set_enabled(thread, args.name, True)
                value["resume_policy"] = "Preserve the checkpoint, retry any pending event first, then report changes since the last baseline."
            else:
                value = monitor.managed_remove(thread, args.name)
            value["receiver_running"] = process_alive(root / "serve.lock")
            output(value)
        elif args.command in ("bind", "attach"):
            if MANAGED_SOURCE in args.source:
                raise ValueError("reserved managed source cannot be registered externally")
            if any(source not in config["sources"] for source in args.source):
                raise ValueError("register each source before binding")
            output(monitor.bind(args.name, args.thread, args.endpoint, args.source))
        elif args.command == "status":
            output({**monitor.status(), "receiver_alive": process_alive(root / "serve.lock")})
        elif args.command == "sessions":
            value = overview(monitor, args.name)
            output(value) if args.json else print(display(value))
        elif args.command == "reply":
            parent = monitor.event(args.delivery_id)
            if parent["source"] == MANAGED_SOURCE:
                raise ValueError("managed file monitor has no external reply recipient; respond in the conversation")
            message = sys.stdin.read(16385) if args.message == "-" else args.message
            output(ReplyStore(root).add(parent, args.id, message))
        elif args.command == "event":
            output(monitor.event(args.id))
        elif args.command == "inspect":
            event = monitor.event(args.id)
            binding = next(item for item in monitor.bindings() if item["name"] == event["binding"])
            try:
                native = pool(binding["endpoint"]).inspect(binding["thread"], event["client_id"])
            except Exception as exc:
                native = {"state": "unknown", "reason": str(exc)}
            output({
                "delivery_id": event["id"],
                "local": {
                    "state": event["state"],
                    "binding": event["binding"],
                    "client_id": event["client_id"],
                    "submission_id": event["submission_id"],
                    "attempts": event["attempts"],
                    "error": event["error"],
                    "created": event["created"],
                    "updated": event["updated"],
                },
                "target": {"thread": binding["thread"], "endpoint": binding["endpoint"]},
                "native": native,
                "read_only": True,
                "note": "Local accepted means Codex storage accepted the message. Native consumed means the client message reached thread history. Neither state proves model completion or success.",
            })
        elif args.command in ("enable", "disable", "pause", "unpause"):
            enabled = args.command in ("enable", "unpause")
            monitor.enable(args.binding, enabled)
            output({"binding": args.binding, "enabled": enabled,
                    "note": "Controls monitor ingress/dispatch only; already accepted Codex input is unchanged."})
        elif args.command == "resolve":
            with ProcessLock(root / "serve.lock"):
                monitor.resolve(args.id, args.action, args.reason)
            output(monitor.event(args.id))
        elif args.command == "serve":
            if MANAGED_SOURCE in config["sources"]:
                raise ValueError("reserved managed source cannot be configured for HTTP credentials")
            tokens = {name: Path(value["token_file"]).read_text().strip() for name, value in config["sources"].items()}
            stopped = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stopped.set())
            with ProcessLock(root / "serve.lock"):
                managed_supervisor = ManagedSupervisor(monitor)
                server = Server(monitor, tokens, (root / "admin.token").read_text().strip(), port=config["port"]).start()
                managed_supervisor.start()
                output({"listening": server.url, "state": str(root)})
                try:
                    stopped.wait()
                finally:
                    managed_supervisor.close()
                    server.close()
        elif args.command in ("send", "watch-file"):
            if not NAME.fullmatch(args.to) or "/" in args.to:
                raise ValueError("invalid target binding")
            if args.source == MANAGED_SOURCE:
                raise ValueError("reserved managed source cannot be used by external producers")
            token_env = getattr(args, "token_env", None)
            token = os.environ[token_env] if token_env else Path(config["sources"][args.source]["token_file"]).read_text().strip()
            url = args.url or f"http://127.0.0.1:{config['port']}"
            if not url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
                raise ValueError("remote webhook targets require https://")
            if args.command == "send":
                data = json.load(sys.stdin) if args.data == "-" else json.loads(args.data)
                event = {"id": args.id or str(uuid.uuid4()), "source": args.source, "type": args.type, "data": data, "hops": args.hops}
                if args.trace: event["trace_id"] = args.trace
                output(send(url, args.to, event, token))
            else:
                if args.interval < .1:
                    raise ValueError("interval must be at least 0.1 seconds")
                key = hashlib.sha256(json.dumps([str(Path(args.file).resolve()), args.to, args.source, url]).encode()).hexdigest()
                stop = threading.Event()
                for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop.set())
                with ProcessLock(root / "watchers" / (key + ".lock")):
                    watcher = ChangeWatcher(root / "watchers" / (key + ".json"), args.source, "monitor.changed", lambda e: send(url, args.to, e, token))
                    while not stop.is_set():
                        try: watcher.check(file_sample(args.file))
                        except (OSError, IngressError) as exc: print(f"watch delivery retained: {exc}", file=sys.stderr)
                        stop.wait(args.interval)
        return 0
    except (ValueError, TypeError, OSError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(f"codex-monitor: {exc}", file=sys.stderr)
        return 2
    finally:
        pool.close()
