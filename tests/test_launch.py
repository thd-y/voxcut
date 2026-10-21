import json
import os
import socket
import threading
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
