"""`make ui` / VoxCut.cmd: one server per product, an Edge InPrivate app window per launch (U1, S1, S2).

The first launch holds .run/ui.lock and serves; a later launch asks it over .run/ui.sock for a
fresh one-time code and only opens another window. Jobs run in systemd units and outlive this.
"""

import argparse
import fcntl
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from voxcut.jobs import Jobs
from voxcut.library import Layout
from voxcut.ui.server import HOST, make_server

EDGE_CANDIDATES = (
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
)
ROOT = Path(__file__).resolve().parents[2]


def edge_path() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("Не найден Microsoft Edge: " + ", ".join(EDGE_CANDIDATES))


def edge_argv(edge: str, port: int, code: str) -> list[str]:
    return [edge, "--inprivate", f"--app=http://{HOST}:{port}/#{code}"]


def _open_window(popen, port: int, code: str) -> None:
    popen(edge_argv(edge_path(), port, code), stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _ask_running(sock_path: Path, popen) -> int:
    """Another launch holds the lock: get a new code from it and open a window."""
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(5)
                s.connect(str(sock_path))
                s.sendall(b"open\n")
                reply = json.loads(s.makefile().readline())
            break
        except (OSError, ValueError):
            if time.monotonic() > deadline:
                raise SystemExit("VoxCut уже запускается или завис: нет ответа от .run/ui.sock")
            time.sleep(0.2)  # the first launch is still starting its server
    _open_window(popen, reply["port"], reply["code"])
    print(f"VoxCut уже работает (127.0.0.1:{reply['port']}): открыто ещё одно окно")
    return 0


def _serve_socket(sock: socket.socket, app) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return  # closed on shutdown
        with conn:
            conn.settimeout(5)
            try:
                if conn.makefile().readline() == "open\n":
                    reply = {"port": app.port, "code": app.new_launch_code()}
                    conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


def main(argv: list[str] | None = None, popen=subprocess.Popen, on_ready=None) -> int:
    ap = argparse.ArgumentParser(prog="voxcut-ui")
    ap.add_argument("--root", default=str(ROOT))
    lay = Layout(Path(ap.parse_args(argv).root).resolve())
    lay.ensure()
    lock_fd = os.open(lay.run / "ui.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return _ask_running(lay.run / "ui.sock", popen)
    sock_path, meta_path = lay.run / "ui.sock", lay.run / "ui.json"
    try:
        sock_path.unlink(missing_ok=True)  # we hold the lock: a leftover socket is stale
        jobs = Jobs(lay)
        server, app = make_server(lay, jobs=jobs)  # busy port -> OSError before any code
        sock = socket.socket(socket.AF_UNIX)
        old = os.umask(0o177)
        try:
            sock.bind(str(sock_path))
        finally:
            os.umask(old)
        sock.listen()
        threading.Thread(target=_serve_socket, args=(sock, app), daemon=True).start()
        _write_private(meta_path, json.dumps({"pid": os.getpid(), "port": app.port}))
        stop_poll = app.start_poller(2.0)
        _open_window(popen, app.port, app.new_launch_code())
        print(f"VoxCut: 127.0.0.1:{app.port}. Окно открыто. Сервер работает, пока открыта "
              f"эта консоль (Ctrl+C — остановить); задачи продолжаются без него.", flush=True)
        if on_ready:
            on_ready(server)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_poll.set()
            sock.close()
            server.server_close()
    finally:
        sock_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
