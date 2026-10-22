import json
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest
from test_server_security import Client

from voxcut.ui import launch


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)

    def opened(self):
        """(port, code) of every window opened."""
        out = []
        for argv in self.calls:
            url = urlsplit(argv[-1].removeprefix("--app="))
            out.append((url.port, url.fragment))
        return out


@pytest.fixture
def edge(monkeypatch):
    monkeypatch.setattr(launch, "edge_path", lambda: "/fake/msedge.exe")


def start_first(tmp_path, popen):
    """main() as the server in a thread; returns the running server."""
    ready = threading.Event()
    box = {}

    def on_ready(server):
        box["server"] = server
        ready.set()

    th = threading.Thread(target=launch.main, args=(["--root", str(tmp_path)],),
                          kwargs={"popen": popen, "on_ready": on_ready}, daemon=True)
    th.start()
    assert ready.wait(10), "сервер не поднялся"
    return box["server"], th


def stop(server, th):
    server.shutdown()
    th.join(5)
    assert not th.is_alive()


def test_edge_argv():
    argv = launch.edge_argv("/e/msedge.exe", 4321, "CODE")
    assert argv == ["/e/msedge.exe", "--inprivate", "--app=http://127.0.0.1:4321/#CODE"]
    assert "?" not in argv[-1]  # the code is in the fragment: never sent to a server (S2)


def test_edge_path_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "EDGE_CANDIDATES", (str(tmp_path / "none.exe"),))
    with pytest.raises(SystemExit, match="Edge"):
        launch.edge_path()


def test_second_instance_gets_new_code(tmp_path, edge):
    p1, p2 = FakePopen(), FakePopen()
    server, th = start_first(tmp_path, p1)
    try:
        called, result = [], []
        second = threading.Thread(target=lambda: result.append(launch.main(
            ["--root", str(tmp_path)], popen=p2, on_ready=called.append)), daemon=True)
        second.start()
        second.join(10)
        if called:
            called[0].shutdown()
        assert called == [] and result == [0]  # no second server: it asked the first one
        (port1, code1), (port2, code2) = p1.opened()[0], p2.opened()[0]
        assert port1 == port2 == server.server_address[1] and code1 != code2
        app = server.RequestHandlerClass.app
        c = Client(app)
        assert c.req("POST", "/api/exchange", {"code": code2})[0] == 200
    finally:
        stop(server, th)
    assert not (tmp_path / ".run" / "ui.sock").exists()
    assert not (tmp_path / ".run" / "ui.json").exists()


def test_stale_socket_recovered(tmp_path, edge):
    run = tmp_path / ".run"
    run.mkdir(mode=0o700)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(run / "ui.sock"))
    s.close()  # the file stays, nobody listens: a crashed server
    p = FakePopen()
    server, th = start_first(tmp_path, p)
    try:
        assert len(p.opened()) == 1
        p2 = FakePopen()
        assert launch.main(["--root", str(tmp_path)], popen=p2) == 0
        assert p2.opened()[0][0] == server.server_address[1]
    finally:
        stop(server, th)


def test_ui_json_no_secrets(tmp_path, edge):
    p = FakePopen()
    server, th = start_first(tmp_path, p)
    try:
        run = tmp_path / ".run"
        meta = json.loads((run / "ui.json").read_text())
        assert meta == {"pid": os.getpid(), "port": server.server_address[1]}
        assert p.opened()[0][1] not in (run / "ui.json").read_text()
        for name in ("ui.json", "ui.sock", "ui.lock"):
            assert (run / name).stat().st_mode & 0o777 == 0o600, name
        assert run.stat().st_mode & 0o777 == 0o700
    finally:
        stop(server, th)


def test_socket_rejects_other_commands(tmp_path, edge):
    server, th = start_first(tmp_path, FakePopen())
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(tmp_path / ".run" / "ui.sock"))
            s.sendall(b"shell rm -rf\n")
            assert s.recv(4096) == b""
    finally:
        stop(server, th)


def test_envcheck_listeners_parsing():
    from voxcut.ui.envcheck import listeners

    ss = ("LISTEN 0 5 127.0.0.1:45633 0.0.0.0:* users:((\"python\",pid=173670,fd=3))\n"
          "LISTEN 0 5 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=1736700,fd=3))\n"
          "LISTEN 0 5 [::]:45633 [::]:* users:((\"python\",pid=173670,fd=5))\n")
    assert listeners(ss, 173670) == ["127.0.0.1:45633", "[::]:45633"]
    assert listeners(ss, 17367) == []


def test_closing_console_stops_server(tmp_path, edge):
    master, slave = os.openpty()
    console = os.fdopen(slave, "r")
    done = []
    th = threading.Thread(target=lambda: done.append(launch.main(
        ["--root", str(tmp_path)], popen=FakePopen(), console=console)), daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not (tmp_path / ".run" / "ui.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    os.close(master)  # the owner closes the VoxCut.cmd window
    th.join(10)
    assert done == [0]
    assert not (tmp_path / ".run" / "ui.json").exists()


@pytest.mark.parametrize("rc, control, ok, text", [
    (3, 0, True, "сеть отрезана"),
    (3, 3, False, "не проверено"), (3, 1, False, "не проверено"), (3, None, False, "не проверено"),
    (0, 0, False, "есть сеть"), (4, 0, False, "другая ошибка"),
    (1, 0, False, "пробник не запустился"), (127, 0, False, "пробник не запустился"),
    (None, 0, False, "таймаут"),
])
def test_envcheck_probe_verdict(rc, control, ok, text):
    """D63: isolated only if the unit sees ENETUNREACH and the same unit without
    PrivateNetwork reaches the network: an offline host or a dead unit proves nothing."""
    from voxcut.ui.envcheck import probe_verdict

    got_ok, got_text = probe_verdict(rc, control)
    assert got_ok is ok and text in got_text


@pytest.mark.parametrize("exc, code", [
    (OSError(101, "Network is unreachable"), 3),  # ENETUNREACH: empty namespace
    (TimeoutError("timed out"), 4),  # an offline host or a firewall, not isolation
    (ConnectionRefusedError(111, "refused"), 4),
])
def test_envcheck_probe_exit_codes(monkeypatch, exc, code):
    import socket

    from voxcut.ui.envcheck import PROBE

    def connect(*a, **k):
        raise exc

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(SystemExit) as e:
        exec(PROBE, {})  # noqa: S102 - our own constant, the same code `python -c` runs
    assert e.value.code == code


def test_envcheck_run_probe_timeout(monkeypatch, tmp_path):
    """A unit that hangs past the timeout is a verdict input (None), not a traceback."""
    import subprocess

    from voxcut.ui import envcheck

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 60)

    monkeypatch.setattr(envcheck.subprocess, "run", hang)
    assert envcheck.run_probe(tmp_path, True) is None
