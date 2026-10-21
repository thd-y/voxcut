"""HTTP server: security core (S1, S2, S4, S5, S8) and a route table for the API.

Every request goes through the same gate in a fixed order: Host, OPTIONS, POST origin and body,
API key, then the route. The client never sends a path: static files come from a fixed table.
"""

import hmac
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from voxcut.journal import Journal
from voxcut.library import Layout

HOST = "127.0.0.1"
CSP = ("default-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'none'; frame-src 'none'; worker-src 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
BODY_MAX = 64 * 1024
DRAIN_MAX = 1024 * 1024
CODE_TTL_S = 30
STATIC_DIR = Path(__file__).parent / "static"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
JSON_TYPE = "application/json; charset=utf-8"


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    ctype: str = JSON_TYPE
    headers: dict = field(default_factory=dict)


@dataclass
class Request:
    method: str
    params: dict
    query: dict
    body: dict | None
    headers: object


class App:
    def __init__(self, lay: Layout, port: int, jobs=None, journal: Journal | None = None):
        self.lay, self.port, self.jobs = lay, port, jobs
        self.journal = journal if journal is not None else Journal(lay.journal)
        self.clock = time.monotonic
        self.log = lambda line: print(line, file=sys.stderr, flush=True)
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}
        self._keys: list[str] = []
        self.routes: list[tuple[str, str, re.Pattern, object]] = []
        self.route("POST", "/api/exchange")(self._exchange)
        self.route("GET", "/api/ping")(lambda req: {"ok": True})

    def route(self, method: str, template: str):
        """Register a handler for templates like /api/session/{sid}; {x} matches one segment."""
        pattern = re.compile("^" + re.sub(r"\\{(\w+)\\}", r"(?P<\1>[^/]+)", re.escape(template))
                             + "$")

        def add(handler):
            self.routes.append((method, template, pattern, handler))
            return handler
        return add

    def new_launch_code(self) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            self._codes[code] = now + CODE_TTL_S
        return code

    def _exchange(self, req: Request) -> dict:
        code = req.body.get("code")
        if not isinstance(code, str):
            raise HttpError(400, "code")
        with self._lock:  # the code dies at its first use, valid or late
            match = next((c for c in self._codes if hmac.compare_digest(c, code)), None)
            expires = self._codes.pop(match, None) if match else None
            if expires is None or self.clock() > expires:
                raise HttpError(403, "код запуска недействителен")
            key = secrets.token_urlsafe(32)
            self._keys.append(key)
        return {"key": key}

    def key_ok(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        given = header.removeprefix("Bearer ")
        with self._lock:
            keys = list(self._keys)
        return any(hmac.compare_digest(given, k) for k in keys) and bool(given)


class Handler(BaseHTTPRequestHandler):
    server_version = "VoxCut"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App

    # Every response, including send_error from the base class, carries the S5 headers.
    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, format, *args):
        pass  # the base class logs the request line with ids and queries (S8)

    def log_request(self, code="-", size="-"):
        self.app.log(f"{self.command} {getattr(self, '_template', '-')} {int(code)}")

    def _send(self, resp: Response) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    def _json(self, status: int, obj) -> Response:
        return Response(status, json.dumps(obj, ensure_ascii=False).encode())

    def _dispatch(self) -> None:
        self._template = "-"
        try:
            resp = self._gate()
        except HttpError as exc:
            resp = self._json(exc.status, {"error": exc.message or str(exc.status)})
            self.close_connection = True  # an unread body must not leak into the next request
        except Exception as exc:  # noqa: BLE001 - the client gets a status, the log a type only
            self.app.log(f"{self.command} {self._template} error {type(exc).__name__}")
            resp = self._json(500, {"error": "внутренняя ошибка сервера"})
            self.close_connection = True
        self._send(resp)

    def _gate(self) -> Response:
        app = self.app
        if self.headers.get("Host") != f"{HOST}:{app.port}":
            raise HttpError(421, "неверный Host")
        if self.command == "OPTIONS":
            raise HttpError(405, "CORS не поддерживается")
        body = None
        if self.command == "POST":
            if (self.headers.get("Origin") != f"http://{HOST}:{app.port}"
                    or self.headers.get("Sec-Fetch-Site") != "same-origin"):
                raise HttpError(403, "чужой источник запроса")
            body = self._read_body()
        url = urlsplit(self.path)
        path = url.path
        if path in STATIC:
            self._template = path
            if self.command not in ("GET", "HEAD"):
                raise HttpError(405, "метод не поддерживается")
            name, ctype = STATIC[path]
            return Response(200, (STATIC_DIR / name).read_bytes(), ctype)
        if (path.startswith("/api/") and path != "/api/exchange"
                and not app.key_ok(self.headers.get("Authorization"))):
            raise HttpError(401, "нужен ключ")
        allowed = []
        for method, template, pattern, handler in app.routes:
            m = pattern.match(path)
            if not m:
                continue
            if method != self.command and not (method == "GET" and self.command == "HEAD"):
                allowed.append(method)
                continue
            self._template = template
            out = handler(Request(self.command, m.groupdict(), parse_qs(url.query), body,
                                  self.headers))
            return out if isinstance(out, Response) else self._json(200, out)
        raise HttpError(405 if allowed else 404, "метод не поддерживается" if allowed
                        else "не найдено")

    def _read_body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(400, "нужен Content-Length")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise HttpError(400, "нужен Content-Length") from None
        if n > BODY_MAX:
            if n <= DRAIN_MAX:  # read it away so the client sees 413, not a reset connection
                self.rfile.read(n)
            raise HttpError(413, "тело больше 64 КиБ")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            raise HttpError(400, "тело не JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "тело не JSON-объект")
        return body

    do_GET = do_HEAD = do_POST = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _dispatch


def make_server(lay: Layout, host: str = HOST, port: int = 0, jobs=None,
                journal: Journal | None = None) -> tuple[ThreadingHTTPServer, App]:
    """S1: loopback only; a busy port raises OSError before any launch code exists."""
    if host != HOST:
        raise ValueError(f"сервер слушает только {HOST}")
    handler = type("BoundHandler", (Handler,), {})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    app = App(lay, server.server_address[1], jobs, journal)
    handler.app = app
    return server, app
