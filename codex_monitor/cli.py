import argparse
import hashlib
import importlib.metadata
import json
import math
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

from .errors import IngressError, Permanent
from .http import Server
from .lock import ProcessLock, process_alive
from .monitor import Monitor, NAME
from .monitor import MANAGED_SOURCE
from .managed import ManagedSupervisor
from .session import SessionPool, Rpc, AppServerSession, server_token
from .service import ServiceManager
from .watch import ChangeWatcher, atomic_json, file_sample
from .replies import ReplyStore
from .requests import RequestStore
from .sessions import overview, display
from .dashboard import run_dashboard
from .doctor import probe_queue_target
from .resident import ResidentKeeper


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


def request_original(monitor, delivery_id, thread):
    """Resolve an external delivery and its immutable conversation scope."""
    event = monitor.event(delivery_id)
    if event["source"] == MANAGED_SOURCE:
        raise ValueError("managed file monitor deliveries cannot track requests")
    binding = next((item for item in monitor.bindings() if item["name"] == event["binding"]), None)
    if binding is None or binding["thread"] != thread:
        raise ValueError("delivery does not belong to the requested conversation")
    return event, binding


def require_receiver_capability(root, config, capability):
    """Fail closed when a live receiver does not advertise a feature."""
    if not process_alive(root / "serve.lock"):
        return
    try:
        token = (root / "admin.token").read_text().strip()
        port = config["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid receiver port")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/status",
            headers={"Authorization": "Bearer " + token},
        )
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=2) as response:
            status = json.load(response)
        capabilities = status.get("capabilities") if isinstance(status, dict) else None
        if not isinstance(capabilities, dict) or capabilities.get(capability) is not True:
            raise ValueError(f"active receiver does not support {capability}; restart it before this operation")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("cannot verify active receiver capabilities; restart it before this operation") from exc


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
    dashboard = commands.add_parser("dashboard", help="read-only inventory of attached conversations")
    dashboard.add_argument("--once", action="store_true", help="render one snapshot and exit")
    dashboard.add_argument("--json", action="store_true", help="emit one JSON snapshot (requires --once)")
    dashboard.add_argument("--interval", type=float, default=2.0, help="live refresh interval in seconds")
    dashboard.add_argument("--thread", help="limit the inventory to one conversation ID")
    dashboard.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                           help="color policy for the terminal dashboard (default: auto)")
    dashboard.add_argument("--no-animate", action="store_true", help="disable the live indicator animation")
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
    create.add_argument("--debounce", type=float, default=0, help="require unchanged observed samples for this many seconds before emitting a change")
    create.add_argument("--json-pointer", help="RFC 6901 pointer for a JSON condition")
    create.add_argument("--operator", choices=["eq", "ne", "gt", "gte", "lt", "lte"])
    create.add_argument("--value", help="finite JSON value for a JSON condition")
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
    connect.add_argument("--thread", help="resume this exact conversation on the selected owner")
    resident = commands.add_parser("resident", help="retain explicit CLI conversations on one shared owner; foreground process")
    resident.add_argument("--endpoint", required=True, help="same owner endpoint used by codex --remote; shared-local is not supported")
    resident.add_argument("--thread", action="append", required=True, help="existing conversation ID; repeat for multiple conversations")
    resident.add_argument("--health-interval", type=float, default=10, help="read-only owner probe interval in seconds; no model calls")
    service = commands.add_parser("service", help="manage the macOS launchd receiver")
    service.add_argument("action", choices=["install", "start", "stop", "restart", "status", "uninstall"])
    service.add_argument("--codex-home", help="Codex storage home (defaults to CODEX_HOME or ~/.codex)")
    request = commands.add_parser("request", help="track explicit work in an existing conversation")
    request_commands = request.add_subparsers(dest="request_action", required=True)
    track = request_commands.add_parser("track")
    track.add_argument("delivery_id"); track.add_argument("--key", required=True)
    track.add_argument("--thread"); track.add_argument("--summary")
    track.add_argument("--expires-in", type=float)
    listing = request_commands.add_parser("list")
    listing.add_argument("--thread")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--after", type=int)
    status = request_commands.add_parser("status")
    status.add_argument("request_id"); status.add_argument("--thread")
    update = request_commands.add_parser("update")
    update.add_argument("request_id"); update.add_argument("--thread")
    update.add_argument("--state", dest="request_state", required=True); update.add_argument("--update-id", required=True)
    update.add_argument("--revision", type=int, required=True); update.add_argument("--message")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(args.state).expanduser().resolve()
    config_path = root / "config.json"
    pool = None
    try:
        if args.command == "resident":
            token = server_token()
            keeper = ResidentKeeper(
                args.endpoint, args.thread, health_interval=args.health_interval,
                rpc_factory=lambda endpoint: Rpc(endpoint, token=token),
            )
            stopped = threading.Event()
            handlers = {}
            errors = []

            def run_keeper():
                try:
                    keeper.run(stopped)
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    stopped.set()

            worker = threading.Thread(target=run_keeper, name="codex-monitor-resident")
            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
                worker.start()
                previous = None
                while True:
                    snapshot = keeper.status()
                    # Probe freshness changes every interval; print actual
                    # transitions instead of filling terminals with heartbeats.
                    comparable = {key: value for key, value in snapshot.items() if key != "last_probe_at"}
                    if comparable != previous:
                        output({"resident": snapshot, "model_polling": False})
                        previous = comparable
                    if stopped.wait(.25):
                        break
            finally:
                stopped.set()
                if worker.ident is not None:
                    worker.join()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            output({"resident": keeper.status(), "stopped": True, "errors": errors})
            return 2 if errors else 0
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
                loaded = None
                if args.thread:
                    if args.endpoint == "shared-local":
                        diagnostic = probe_queue_target(
                            rpc,
                            args.thread,
                            endpoint=args.endpoint,
                            requested_surface=args.surface,
                        )
                        if diagnostic is not None:
                            output(diagnostic)
                            return 2
                    else:
                        loaded = rpc.call("thread/loaded/list", {})["data"]
                        AppServerSession(rpc).check_target(args.thread)
                        diagnostic = probe_queue_target(
                            rpc,
                            args.thread,
                            endpoint=args.endpoint,
                            requested_surface=args.surface,
                        )
                        if diagnostic is not None:
                            output(diagnostic)
                            return 2
                queue_ready = bool(args.thread)
                consumer_ready = "unknown" if args.endpoint == "shared-local" else bool(args.thread)
                if not args.thread:
                    delivery_guarantee = "target not checked; provide --thread for a queue compatibility probe"
                elif args.endpoint == "shared-local":
                    delivery_guarantee = "queue target is readable; the owning local client consumer remains unverified"
                else:
                    delivery_guarantee = "queue target is readable and loaded in this App Server"
                output({"ready": bool(args.thread), "endpoint": args.endpoint, "requested_surface": args.surface,
                        "level": ("shared-queue-ready" if args.endpoint == "shared-local" else "protocol-ready") if args.thread else "endpoint-only", "client_ui_verified": False,
                        "loaded_thread_ids": loaded, "queue_api": {"ready": queue_ready, "method": "thread/queue/list"},
                        "consumer_ready": consumer_ready, "delivery_guarantee": delivery_guarantee,
                        "note": "The selected local client must use the same Codex storage; its own server consumes this queue." if args.endpoint == "shared-local" else "The selected client must be using this exact App Server and thread."})
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
            command = ["codex", "--remote", args.endpoint or endpoint, "-C", args.cwd]
            token = server_token()
            if token is not None:
                os.environ["CODEX_MONITOR_SERVER_TOKEN"] = token
                command += ["--remote-auth-token-env", "CODEX_MONITOR_SERVER_TOKEN"]
            if args.thread:
                command += ["resume", args.thread]
            os.execvp("codex", command)
        if args.command == "dashboard":
            # The dashboard does not need configuration to read the local
            # inventory.  A missing or malformed config only makes the
            # receiver readiness probe unavailable; it must not turn a
            # useful read-only snapshot into a state-changing initialization.
            return run_dashboard(
                root,
                once=args.once,
                as_json=args.json,
                interval=args.interval,
                thread=args.thread,
                color=args.color,
                animate=not args.no_animate,
            )
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
        pool = SessionPool()
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
        if args.command == "request":
            thread = monitor_thread(args.thread)
            if args.request_action in ("track", "update"):
                require_receiver_capability(root, config, "request_lifecycle")
            store = RequestStore(root, clock=monitor.clock)
            if args.request_action == "track":
                event, binding = request_original(monitor, args.delivery_id, thread)
                if args.expires_in is not None and (args.expires_in < 0 or not math.isfinite(args.expires_in)):
                    raise ValueError("expires-in must be a finite non-negative number")
                payload = {"summary": args.summary} if args.summary is not None else {}
                value = store.create(
                    thread, event["source"], args.key, event["id"], event["binding"], payload,
                    expires_in=args.expires_in,
                )
            elif args.request_action == "list":
                value = store.list_requests(thread, limit=args.limit, after=args.after)
            else:
                value = store.get_by_id(args.request_id, conversation_id=thread)
                if args.request_action == "update":
                    summary = {"message": args.message} if args.message is not None else None
                    value = store.transition(
                        thread, value["source"], value["request_key"],
                        update_id=args.update_id, target_state=args.request_state,
                        expected_revision=args.revision, summary=summary,
                    )
            output(value)
        elif args.command == "monitor":
            thread = monitor_thread(args.thread)
            if args.monitor_action == "create":
                path = os.path.abspath(os.path.expanduser(args.file))
                provided = (args.json_pointer is not None, args.operator is not None, args.value is not None)
                if any(provided) and not all(provided):
                    raise ValueError("--json-pointer, --operator and --value must be provided together")
                condition = None
                if all(provided):
                    try:
                        expected = json.loads(args.value)
                    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
                        raise ValueError("--value must be finite JSON") from exc
                    condition = {"pointer": args.json_pointer, "operator": args.operator, "value": expected}
                    require_receiver_capability(root, config, "managed_json_predicates")
                value = monitor.managed_create(
                    thread, args.name, path, args.interval, args.endpoint,
                    debounce_seconds=args.debounce, condition=condition,
                )
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
                "processing": {
                    "state": {"queued": "awaiting_native_consumption", "consumed": "native_consumed"}.get(native["state"], "unknown"),
                    "accepted_age_seconds": max(0, monitor.clock() - event["updated"]) if event["state"] == "accepted" else None,
                    "consumer_presence": "unknown",
                    "work_completion_verified": False,
                    "automatic_replay_safe": False,
                    "guidance": "Queued input needs an owning loaded Codex conversation. An unloaded target does not self-start from a shared-local queue write. Check the exact target in its owning client and the shared storage; do not resend accepted input." if native["state"] == "queued" else "Native history and local acceptance do not verify the requested work or its reply.",
                },
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
    except (ValueError, TypeError, OSError, KeyError, RuntimeError, sqlite3.Error, Permanent) as exc:
        print(f"codex-monitor: {exc}", file=sys.stderr)
        return 2
    finally:
        if pool is not None:
            pool.close()
