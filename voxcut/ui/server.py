"""HTTP server: security core (S1, S2, S4, S5, S8) and a route table for the API.

Every request goes through the same gate in a fixed order: Host, OPTIONS, POST origin and body,
API key, then the route. The client never sends a path: static files come from a fixed table.
"""

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, urlsplit

from voxcut import journal as jr
from voxcut import library
from voxcut.jobs import JobError
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
    "/vendor/basecoat-luma.css": ("vendor/basecoat-luma.css", "text/css; charset=utf-8"),
    "/vendor/lucide-sprite.svg": ("vendor/lucide-sprite.svg", "image/svg+xml"),
}
JSON_TYPE = "application/json; charset=utf-8"


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Response:
    status: int = 200
    body: bytes | BinaryIO = b""  # an open file is streamed and closed by _send
    ctype: str = JSON_TYPE
    headers: dict = field(default_factory=dict)


@dataclass
class Request:
    method: str
    params: dict
    query: dict
    body: dict | None
    headers: object


def _log(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:
        pass  # a closed console must not cost the request its response


def open_folder(path: Path, run=subprocess.run, popen=subprocess.Popen) -> None:
    """D49: show a fixed folder in Windows Explorer; its exit code means nothing, so not awaited."""
    path.mkdir(exist_ok=True)
    win = run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    popen(["explorer.exe", win], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, start_new_session=True)


class App:
    def __init__(self, lay: Layout, port: int, jobs=None, journal: Journal | None = None):
        self.lay, self.port, self.jobs = lay, port, jobs
        self.open_folder = open_folder
        self.journal = journal if journal is not None else Journal(lay.journal)
        self.clock = time.monotonic
        self.log = _log
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}
        self._keys: list[str] = []
        self.routes: list[tuple[str, str, re.Pattern, object]] = []
        self.clips: dict[str, tuple[str, float, float, Path | None]] = {}  # S6: id -> file
        self.route("POST", "/api/exchange")(self._exchange)
        self.route("GET", "/api/ping")(lambda req: {"ok": True})
        self.route("GET", "/api/state")(self._state)
        self.route("GET", "/api/session/{sid}")(self._session)
        self.route("GET", "/api/clip/{clip_id}")(self._clip)
        self.route("POST", "/api/journal")(self._journal_post)
        self.route("POST", "/api/jobs")(self._jobs_post)
        self.route("POST", "/api/jobs/{job_id}/cancel")(self._jobs_cancel)
        self.route("POST", "/api/inbox/open")(self._inbox_open)

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

    def _state(self, req: Request) -> dict:
        events = self.journal.events()
        return {
            "sessions": library.list_sessions(self.lay),
            "inbox": library.inbox_inventory(self.lay),
            "jobs": self.jobs.list() if self.jobs is not None else [],
            "last_backup": jr.last_backup(events),
            "ideas": jr.ideas(events),
            "journal_warnings": list(self.journal.warnings),
        }

    def _session(self, req: Request) -> dict:
        sid = req.params["sid"]
        rows = {r["session_id"]: r for r in library.list_sessions(self.lay)}
        if not library.SESSION_ID_RE.fullmatch(sid) or sid not in rows:
            raise HttpError(404, "нет такой сессии")
        runs = library.session_runs(self.lay, sid)
        wanted = req.query.get("run", [None])[0]
        run = next((r for r in runs if r["run_id"] == wanted), None) if wanted else (
            runs[0] if runs else None)
        if wanted and run is None:
            raise HttpError(404, "нет такого прогона")
        sj = self.lay.sessions / sid / f"{sid}.session" / "session.json"
        files = library.load_json(sj).get("files") if sj.is_file() else None
        events = self.journal.events()
        clips = []
        for row in library.clip_table(self.lay, run["seg_dir"]) if run else []:
            s, e = float(row["start_s"]), float(row["end_s"])
            with self._lock:
                self.clips[row["clip_id"]] = (sid, s, e, row["path"])
            clips.append({"clip_id": row["clip_id"], "n": row["n"], "start_s": s, "end_s": e,
                          "start_local": row.get("start_local"),
                          "duration_s": row.get("duration_s", e - s), "p_max": row.get("p_max")}
                         | jr.clip_view(events, sid, s, e))
        return {
            "session_id": sid,
            "start_local": files[0].get("start_local") if files else None,
            "duration_s": rows[sid]["duration_s"],
            "runs": [{k: r[k] for k in ("run_id", "name", "created")} for r in runs],
            "run_id": run["run_id"] if run else None,
            "clips": clips,
        }

    def _clip(self, req: Request) -> Response:
        if req.method != "GET":
            raise HttpError(405, "только GET")
        with self._lock:
            known = self.clips.get(req.params["clip_id"])
        if known is None or known[3] is None:
            raise HttpError(404, "нет такого клипа")
        path = known[3]
        try:
            f = open(path, "rb")  # noqa: SIM115 - _send streams and closes it
        except OSError:
            raise HttpError(404, "нет такого клипа") from None  # no path in the reply or log
        ctype = "audio/flac" if path.suffix.lower() == ".flac" else "audio/wav"
        return Response(200, f, ctype)  # Range is ignored: always the whole file (S3)

    def _journal_post(self, req: Request) -> dict:
        b = req.body
        kind = b.get("kind")
        if kind in ("mark", "note", "heard"):
            if set(b) != {"event_id", "kind", "value", "clip_id"}:
                raise HttpError(400, "поля: event_id, kind, value, clip_id")
            with self._lock:
                known = self.clips.get(b["clip_id"]) if isinstance(b["clip_id"], str) else None
            if known is None:
                raise HttpError(400, "неизвестный clip_id")
            if kind == "heard" and b["value"] != "":
                raise HttpError(400, "heard: value пустой")
            event = {"event_id": b["event_id"], "kind": kind, "value": b["value"],
                     "session_id": known[0], "start_s": known[1], "end_s": known[2]}
        elif kind == "idea":
            if not {"event_id", "kind", "value"} <= set(b) <= {"event_id", "kind", "value",
                                                              "session_id"}:
                raise HttpError(400, "поля: event_id, kind, value, session_id?")
            if "session_id" in b and b["session_id"] not in {
                    r["session_id"] for r in library.list_sessions(self.lay)}:
                raise HttpError(400, "нет такой сессии")
            event = dict(b)
        else:
            raise HttpError(400, "kind: mark, note, heard или idea")
        try:
            saved = self.journal.append(event)  # returns after fsync (U5)
        except jr.JournalError as exc:
            raise HttpError(400, str(exc)) from None
        return {"saved": True, "event": saved}

    def _inbox_open(self, req: Request) -> dict:
        if set(req.body) != set():
            raise HttpError(400, "тело: {}")
        try:
            self.open_folder(self.lay.inbox)  # D49: the path is fixed here, never from the request
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HttpError(503, f"проводник недоступен: {exc}") from None
        return {"ok": True}

    def _jobs_post(self, req: Request) -> dict:
        b = req.body
        if self.jobs is None:
            raise HttpError(503, "задачи недоступны")
        if set(b) != {"job_id", "kind", "params"}:
            raise HttpError(400, "поля: job_id, kind, params")
        try:
            return {"job": self.jobs.submit(b["job_id"], b["kind"], b["params"])}
        except (ValueError, TypeError) as exc:
            raise HttpError(400, str(exc)) from None
        except JobError as exc:
            raise HttpError(409, str(exc)) from None

    def _jobs_cancel(self, req: Request) -> dict:
        if self.jobs is None:
            raise HttpError(503, "задачи недоступны")
        try:
            return {"job": self.jobs.cancel(req.params["job_id"])}
        except KeyError:
            raise HttpError(404, "нет такой задачи") from None

    def on_done(self, job: dict) -> None:
        """Results of jobs that belong in the journal; the server is its only writer (U5)."""
        if job["kind"] == "backup":
            eid = hashlib.sha256((job["job_id"] + "backup").encode()).hexdigest()[:32]
            self.journal.append({"event_id": eid, "kind": "backup", "value": job["result"]})
        elif job["kind"] == "restore":
            events, _ = jr.load(self.lay.jobs / job["job_id"] / "journal.jsonl")
            self.journal.merge(events)

    def start_poller(self, interval: float = 2.0) -> threading.Event:
        """jobs.poll() off the request path: GET never starts anything (S4)."""
        stop = threading.Event()

        def loop():
            while not stop.wait(interval):
                try:
                    self.jobs.poll()
                except Exception as exc:  # noqa: BLE001 - the poller must outlive one bad poll
                    self.log(f"poll error {type(exc).__name__}")

        threading.Thread(target=loop, daemon=True, name="jobs-poll").start()
        return stop

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
        is_file = not isinstance(resp.body, bytes)
        with resp.body if is_file else contextlib.nullcontext() as f:
            try:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.ctype)
                self.send_header("Content-Length",
                                 str(os.fstat(f.fileno()).st_size if is_file else len(resp.body)))
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    if is_file:
                        shutil.copyfileobj(f, self.wfile, 1 << 20)
                    else:
                        self.wfile.write(resp.body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the page aborted the fetch (←/→): not an error

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


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """The stock handler prints a traceback with paths and data to stderr (S8)."""
        exc = sys.exc_info()[1]
        self.RequestHandlerClass.app.log(f"connection error {type(exc).__name__}")


def make_server(lay: Layout, host: str = HOST, port: int = 0, jobs=None,
                journal: Journal | None = None) -> tuple[ThreadingHTTPServer, App]:
    """S1: loopback only; a busy port raises OSError before any launch code exists."""
    if host != HOST:
        raise ValueError(f"сервер слушает только {HOST}")
    handler = type("BoundHandler", (Handler,), {})
    server = Server((host, port), handler)
    server.daemon_threads = True
    app = App(lay, server.server_address[1], jobs, journal)
    if jobs is not None:
        jobs.on_done = app.on_done
    handler.app = app
    return server, app
