"""Loopback ingress for webhooks, agents and external monitoring adapters."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .errors import IngressError
from .monitor import MANAGED_SOURCE
from .replies import ReplyStore
from .requests import RequestStore
from .request_dispatch import RequestDispatcher
from .sessions import overview


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject overload before allocating another request handler thread."""

    def __init__(self, address, handler, max_connections):
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                # A client may still be writing its request when admission is
                # rejected.  Drain only bounded request bytes before closing;
                # closing with unread data can produce a TCP RST and make a
                # normal sender observe BrokenPipe instead of the promised
                # retryable 503.
                deadline = time.monotonic() + .25
                buffered = bytearray()
                while b"\r\n\r\n" not in buffered and len(buffered) <= 65536:
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        break
                    request.settimeout(remaining_time)
                    try:
                        chunk = request.recv(min(4096, 65536 - len(buffered)))
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buffered.extend(chunk)
                header_end = buffered.find(b"\r\n\r\n")
                remaining = 0
                if header_end >= 0:
                    headers = bytes(buffered[:header_end]).split(b"\r\n")[1:]
                    for header in headers:
                        name, separator, value = header.partition(b":")
                        if separator and name.lower() == b"content-length":
                            try:
                                remaining = min(max(int(value.strip()), 0), 32768)
                            except ValueError:
                                remaining = 0
                            remaining = max(0, remaining - len(buffered) + header_end + 4)
                            break
                    while remaining and time.monotonic() < deadline:
                        request.settimeout(max(0.001, deadline - time.monotonic()))
                        try:
                            chunk = request.recv(min(4096, remaining))
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        remaining -= len(chunk)
                remaining_time = deadline - time.monotonic()
                if remaining_time > 0:
                    body = b'{"error":"receiver busy; retry with the same event id"}'
                    request.settimeout(remaining_time)
                    request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                    b"Content-Type: application/json\r\n"
                                    b"Connection: close\r\nRetry-After: 1\r\n"
                                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Server:
    def __init__(self, monitor, sources, admin_token, host="127.0.0.1", port=8766,
                 max_connections=32):
        if not isinstance(max_connections, int) or max_connections < 1:
            raise ValueError("max_connections must be a positive integer")
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("bind loopback and use an authenticated TLS reverse proxy for remote webhooks")
        if any(name == MANAGED_SOURCE for name in sources):
            raise ValueError("reserved managed source cannot be exposed through HTTP credentials")
        if any(not v for v in sources.values()) or not admin_token:
            raise ValueError("admin and registered source tokens must be nonempty; the source registry may be empty")
        if len(set([*sources.values(), admin_token])) != len(sources) + 1:
            raise ValueError("each source and admin need distinct tokens")
        self.monitor = monitor
        self.sources = sources
        self.admin = admin_token
        self.replies = ReplyStore(monitor.root)
        self.requests = RequestStore(monitor.root, clock=monitor.clock)
        self.request_dispatcher = RequestDispatcher(monitor, self.requests)
        self.stop = threading.Event()
        self.worker = None
        self.worker_error = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except (TimeoutError, ConnectionError):
                    self.close_connection = True

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_):
                pass  # Never write headers, secrets or event bodies to request logs.

            def reply(self, code, value):
                encoded = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def token(self):
                value = self.headers.get("Authorization", "")
                return value[7:] if value.startswith("Bearer ") else ""

            def source(self):
                token = self.token()
                source = next((name for name, secret in owner.sources.items()
                               if hmac.compare_digest(token.encode(), secret.encode())), None)
                if source is None:
                    raise IngressError("source credentials required", 401)
                return source

            def body(self, label):
                if self.headers.get("Transfer-Encoding"):
                    raise IngressError("chunked requests are not supported", 400)
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    size = -1
                if size < 0:
                    raise IngressError("Content-Length required", 411)
                if size > 32768:
                    raise IngressError(f"{label} exceeds 32 KiB", 413)
                if self.headers.get_content_type() != "application/json":
                    raise IngressError("Content-Type must be application/json", 415)
                return json.loads(self.rfile.read(size))

            def original(self, source, delivery_id):
                if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 200:
                    raise IngressError("delivery_id must be a nonempty identifier", 400)
                event = owner.monitor.event(delivery_id)
                if event["source"] != source:
                    raise IngressError("delivery not found", 404)
                return event

            @staticmethod
            def query_values(query, allowed):
                unexpected = sorted(set(query) - set(allowed))
                if unexpected:
                    raise IngressError("unknown query parameter", 400)
                values = {}
                for name in allowed:
                    items = query.get(name)
                    if items is None:
                        continue
                    if len(items) != 1:
                        raise IngressError(f"query parameter {name} must occur once", 400)
                    values[name] = items[0]
                return values

            @staticmethod
            def request_view(value):
                """Do not expose store-wide activity counts to one source."""
                if "data" in value:
                    return {**value, "data": [Handler.request_view(item) for item in value["data"]]}
                capacity = value.get("capacity", {})
                return {**value, "capacity": {key: capacity[key] for key in (
                    "updates_used", "updates_limit", "updates_remaining",
                ) if key in capacity}}

            def handle_request(self, write=False):
                try:
                    if self.headers.get("Origin"):
                        raise IngressError("browser origins are not accepted", 403)
                    parsed = urlsplit(self.path)
                    path = parsed.path
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if path == "/v1/replies" or path.startswith("/v1/replies/"):
                        source = self.source()
                        if not write and path == "/v1/replies":
                            self.reply(200, owner.replies.pending(source))
                        elif write and path.endswith("/ack"):
                            reply_id = path[len("/v1/replies/"):-len("/ack")]
                            if not reply_id or "/" in reply_id:
                                raise IngressError("unknown route", 404)
                            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                                raise IngressError("reply acknowledgement takes an empty body")
                            self.reply(200, owner.replies.ack(source, reply_id))
                        else:
                            raise IngressError("unknown route", 404)
                        return
                    if path == "/v1/requests" and write:
                        source = self.source()
                        self.query_values(query, ())
                        data = self.body("request")
                        if not isinstance(data, dict):
                            raise IngressError("request body must be an object")
                        event = self.original(source, data.get("delivery_id"))
                        binding = next((item for item in owner.monitor.bindings()
                                        if item["name"] == event["binding"]), None)
                        if binding is None:
                            raise IngressError("delivery binding not found", 404)
                        value = owner.requests.create(
                            binding["thread"], source, data.get("request_key"),
                            event["id"], event["binding"], data.get("payload", {}),
                            expires_at=data.get("expires_at"),
                        )
                        self.reply(201, self.request_view(value))
                        return
                    if path.startswith("/v1/requests/") and not write and "/" not in path[len("/v1/requests/"):]:
                        source = self.source()
                        self.query_values(query, ())
                        self.reply(200, self.request_view(owner.requests.get_by_id(
                            path[len("/v1/requests/"):], source=source,
                        )))
                        return
                    if path == "/v1/requests" and not write:
                        source = self.source()
                        values = self.query_values(query, {"thread", "limit", "after"})
                        thread = values.get("thread")
                        if not thread:
                            raise IngressError("thread query parameter is required", 400)
                        limit = values.get("limit")
                        if limit is not None:
                            try:
                                limit = int(limit)
                            except (TypeError, ValueError):
                                raise IngressError("limit query parameter must be an integer", 400)
                        self.reply(200, self.request_view(owner.requests.list_requests(
                            thread, source=source, limit=100 if limit is None else limit,
                            after=values.get("after"),
                        )))
                        return
                    if path.startswith("/v1/requests/") and write and path.endswith("/updates"):
                        request_id = path[len("/v1/requests/"):-len("/updates")]
                        if not request_id or "/" in request_id:
                            raise IngressError("unknown route", 404)
                        source = self.source()
                        self.query_values(query, ())
                        request = owner.requests.get_by_id(request_id, source=source)
                        data = self.body("request update")
                        if not isinstance(data, dict):
                            raise IngressError("request update body must be an object")
                        detail = data.get("detail")
                        summary = {"message": detail} if detail is not None else None
                        value = owner.requests.transition(
                            request["conversation_id"], source, request["request_key"],
                            update_id=data.get("update_id"), target_state=data.get("state"),
                            expected_revision=data.get("expected_revision"), summary=summary,
                        )
                        self.reply(200, self.request_view(value))
                        return
                    if write:
                        source = self.source()
                        prefix = "/v1/events/"
                        if not path.startswith(prefix) or "/" in path[len(prefix):]:
                            raise IngressError("unknown route", 404)
                        if self.headers.get("Transfer-Encoding"):
                            raise IngressError("chunked requests are not supported", 400)
                        try:
                            size = int(self.headers.get("Content-Length", "-1"))
                        except ValueError:
                            size = -1
                        if size < 0:
                            raise IngressError("Content-Length required", 411)
                        if size > 32768:
                            raise IngressError("event exceeds 32 KiB", 413)
                        if self.headers.get_content_type() != "application/json":
                            raise IngressError("Content-Type must be application/json", 415)
                        data = json.loads(self.rfile.read(size))
                        if not isinstance(data, dict) or data.get("source") != source:
                            raise IngressError("credential source does not match event source", 403)
                        self.reply(202, owner.monitor.ingest(path[len(prefix):], data))
                    else:
                        if not hmac.compare_digest(self.token().encode(), owner.admin.encode()):
                            raise IngressError("admin credentials required", 401)
                        if path == "/v1/status":
                            self.reply(200, {
                                **owner.monitor.status(), "worker_error": owner.worker_error,
                                "capabilities": {
                                    "request_lifecycle": True,
                                    "managed_json_predicates": True,
                                },
                            })
                        elif path == "/v1/sessions":
                            self.reply(200, overview(owner.monitor))
                        elif path.startswith("/v1/deliveries/"):
                            self.reply(200, owner.monitor.event(path.removeprefix("/v1/deliveries/")))
                        else:
                            raise IngressError("unknown route", 404)
                except IngressError as exc:
                    self.reply(exc.status, {"error": str(exc)})
                except KeyError:
                    self.reply(404, {"error": "delivery not found"})
                except (ValueError, UnicodeError, RecursionError):
                    self.reply(400, {"error": "invalid JSON"})
                except (TimeoutError, ConnectionError, BrokenPipeError):
                    self.close_connection = True
                except Exception:
                    self.reply(503, {"error": "local persistence unavailable; retry with the same event id"})

            def do_POST(self):
                self.handle_request(write=True)

            def do_GET(self):
                self.handle_request()

        self.http = BoundedHTTPServer((host, port), Handler, max_connections)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, name="codex-monitor-http", daemon=True)
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def start(self, dispatch=True):
        self.thread.start()
        if dispatch:
            self.worker = threading.Thread(target=self._dispatch, name="codex-monitor-dispatch", daemon=True)
            self.worker.start()
        return self

    def _dispatch(self):
        while not self.stop.is_set():
            errors = []
            try:
                request_processed = self.request_dispatcher.pump()
            except Exception as exc:
                request_processed = False
                errors.append(("request", exc))
            try:
                monitor_processed = self.monitor.dispatch_once()
            except Exception as exc:
                monitor_processed = False
                errors.append(("monitor", exc))
            if errors:
                processed = False
                if len(errors) == 1:
                    self.worker_error = type(errors[0][1]).__name__
                else:
                    self.worker_error = "; ".join(
                        f"{label}:{type(exc).__name__}" for label, exc in errors
                    )
            else:
                processed = request_processed or monitor_processed
                self.worker_error = None
            if not processed:
                self.monitor.wakeup.wait(.5)
                self.monitor.wakeup.clear()

    def close(self):
        self.stop.set()
        self.monitor.wakeup.set()
        if self.thread.is_alive():
            self.http.shutdown()
            self.thread.join(timeout=5)
        self.http.server_close()
        if self.worker:
            self.worker.join(timeout=15)
