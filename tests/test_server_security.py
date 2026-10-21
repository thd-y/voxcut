import http.client
import json
import socket
import subprocess
import sys
import threading

import pytest

from voxcut.library import Layout
from voxcut.ui.server import CSP, make_server

SECURITY = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def test_csp_verbatim():
    assert CSP == ("default-src 'self'; media-src 'self' blob:; object-src 'none'; "
                   "base-uri 'none'; frame-ancestors 'none'; form-action 'none'; "
                   "frame-src 'none'; worker-src 'none'")


class Client:
    def __init__(self, app):
        self.app, self.port = app, app.port
        self.origin = f"http://127.0.0.1:{self.port}"

    def req(self, method, path, body=None, headers=None, host=True, origin=True, key=None):
        h = {}
        if host is True:
            h["Host"] = f"127.0.0.1:{self.port}"
        elif host:
            h["Host"] = host
        if method == "POST" and origin is True:
            h |= {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        h |= headers or {}
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h.setdefault("Content-Type", "application/json")
            h["Content-Length"] = str(len(data))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def key(self):
        status, _, body = self.req("POST", "/api/exchange", {"code": self.app.new_launch_code()})
        assert status == 200
        return json.loads(body)["key"]


@pytest.fixture
def served(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    server, app = make_server(lay)
    logs = []
    app.log = logs.append
    th = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    th.start()
    yield Client(app), logs
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "localhost", ""])
def test_bind_only_loopback(tmp_path, host):
    with pytest.raises(ValueError):
        make_server(Layout(tmp_path), host=host)


def test_busy_port_refused_no_code(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        with pytest.raises(OSError):
            make_server(Layout(tmp_path), port=s.getsockname()[1])


@pytest.mark.parametrize("host", ["localhost:{p}", "127.0.0.1:{q}", "evil.com", "127.0.0.1", None])
def test_wrong_host_421(served, host):
    c, _ = served
    h = None if host is None else host.format(p=c.port, q=c.port + 1)
    status, headers, _ = c.req("GET", "/", host=h if h is not None else False)
    assert status == 421
    assert all(headers.get(k) == v for k, v in SECURITY.items())


@pytest.mark.parametrize("case", ["no_origin", "null", "localhost", "cross_site", "no_sfs"])
def test_post_origin_checks(served, case):
    c, _ = served
    good = {"Origin": c.origin, "Sec-Fetch-Site": "same-origin"}
    h = {
        "no_origin": {"Sec-Fetch-Site": "same-origin"},
        "null": {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        "localhost": {"Origin": f"http://localhost:{c.port}", "Sec-Fetch-Site": "same-origin"},
        "cross_site": {"Origin": c.origin, "Sec-Fetch-Site": "cross-site"},
        "no_sfs": {"Origin": c.origin},
    }[case]
    assert set(h) <= set(good)
    status, _, _ = c.req("POST", "/api/exchange", {"code": c.app.new_launch_code()},
                         headers=h, origin=False)
    assert status == 403


def test_exchange_once(served):
    c, _ = served
    code = c.app.new_launch_code()
    assert len(code) >= 22  # >= 128 bit in urlsafe base64
    s1, _, b1 = c.req("POST", "/api/exchange", {"code": code})
    s2, _, _ = c.req("POST", "/api/exchange", {"code": code})
    assert s1 == 200 and json.loads(b1)["key"] and s2 == 403
    assert c.req("POST", "/api/exchange", {"code": "x" * 22})[0] == 403


def test_exchange_expired(served):
    c, _ = served
    code = c.app.new_launch_code()
    real = c.app.clock
    c.app.clock = lambda: real() + 31
    assert c.req("POST", "/api/exchange", {"code": code})[0] == 403


def test_two_windows_two_keys(served):
    c, _ = served
    k1, k2 = c.key(), c.key()
    assert k1 != k2
    assert c.req("GET", "/api/ping", key=k1)[0] == 200
    assert c.req("GET", "/api/ping", key=k2)[0] == 200


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic x", "Bearer "])
def test_api_without_key_401(served, auth):
    c, _ = served
    status, _, _ = c.req("GET", "/api/ping", headers={"Authorization": auth} if auth else None)
    assert status == 401


def test_options_no_cors(served):
    c, _ = served
    status, headers, _ = c.req("OPTIONS", "/api/ping", headers={
        "Origin": "http://evil.com", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true"})
    assert status == 405
    assert not any(k.lower().startswith("access-control-") for k in headers)


def test_unknown_method_has_headers(served):
    c, _ = served
    status, headers, _ = c.req("PUT", "/")
    assert status in (405, 501)
    assert all(headers.get(k) == v for k, v in SECURITY.items())


def test_security_headers_everywhere(served):
    c, _ = served
    key = c.key()
    cases = [
        c.req("GET", "/"),
        c.req("GET", "/api/ping", key=key),
        c.req("GET", "/api/ping"),
        c.req("GET", "/nope"),
        c.req("GET", "/api/nope", key=key),
        c.req("POST", "/api/exchange", b"x" * (64 * 1024 + 1)),
        c.req("POST", "/api/exchange", b"[1]"),
    ]
    assert [s for s, _, _ in cases] == [200, 200, 401, 404, 404, 413, 400]
    for _, headers, _ in cases:
        assert {k: headers.get(k) for k in SECURITY} == SECURITY
    assert cases[1][1]["Content-Type"] == "application/json; charset=utf-8"


def test_static_fixed_table(served):
    c, _ = served
    status, headers, body = c.req("GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"<script" not in body or b'src="/app.js"' in body
    for path in ("/index.html", "/static/index.html", "/../pyproject.toml", "/app.js/../x"):
        assert c.req("GET", path)[0] == 404


def test_body_limit_413(served):
    c, _ = served
    assert c.req("POST", "/api/exchange", b"{" + b" " * (64 * 1024) + b"}")[0] == 413
    assert c.req("POST", "/api/exchange", b'{"code": "' + b"a" * 1000 + b'"}')[0] == 403


def test_get_never_mutates(served):
    c, _ = served
    code = c.app.new_launch_code()
    assert c.req("GET", f"/api/exchange?code={code}")[0] in (401, 405)
    assert c.req("POST", "/api/exchange", {"code": code})[0] == 200


def test_log_has_no_secrets(served):
    c, logs = served
    code = c.app.new_launch_code()
    _, _, body = c.req("POST", "/api/exchange", {"code": code})
    key = json.loads(body)["key"]
    c.req("GET", "/api/ping?secret=zzz", key=key)
    c.req("GET", "/api/ping", headers={"Authorization": "Bearer wrong-key-text"})
    c.req("POST", "/api/exchange", {"code": "note text сырой"})
    text = "\n".join(logs)
    assert logs and "POST /api/exchange 200" in text and "GET /api/ping 200" in text
    for secret in (code, key, "zzz", "wrong-key-text", "сырой"):
        assert secret not in text


def test_server_does_not_import_torch():
    code = "import sys, voxcut.ui.server; print('torch' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
