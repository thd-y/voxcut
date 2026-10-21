import re
import threading

import pytest
from test_server_security import SECURITY, Client

from voxcut.library import Layout
from voxcut.ui.server import STATIC, STATIC_DIR, make_server

FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
             "new Function", 'setAttribute("style"', "setAttribute('style'", "localStorage",
             "sessionStorage", "document.cookie", "http://", "https://", "localhost"]


def static_files():
    return sorted(p for p in STATIC_DIR.iterdir() if p.is_file())


@pytest.mark.parametrize("path", static_files(), ids=lambda p: p.name)
def test_static_rules(path):
    text = path.read_text()
    found = [w for w in FORBIDDEN if w in text]
    assert found == [], f"{path.name}: {found}"
    if path.suffix == ".html":
        assert not re.search(r"\son[a-z]+\s*=", text, re.IGNORECASE), "inline event handler"
        assert not re.search(r"\sstyle\s*=", text, re.IGNORECASE), "inline style"
        assert not re.search(r"<script[^>]*>\s*[^<\s]", text, re.IGNORECASE), "inline script body"
        assert re.findall(r'<script[^>]*src="([^"]+)"', text) == ["/app.js"]
        assert re.findall(r'<link[^>]*href="([^"]+)"', text) == ["/app.css"]


def test_static_table_covers_all_files():
    assert {name for name, _ in STATIC.values()} == {p.name for p in static_files()}


def test_index_served_with_csp(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    server, app = make_server(lay)
    app.log = lambda line: None
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    try:
        c = Client(app)
        for path in ("/", "/app.js", "/app.css"):
            status, headers, _ = c.req("GET", path)
            assert status == 200 and {k: headers.get(k) for k in SECURITY} == SECURITY
    finally:
        server.shutdown()
        server.server_close()
