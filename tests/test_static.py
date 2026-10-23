import hashlib
import re
import threading

import pytest
from test_server_security import SECURITY, Client

from voxcut.library import Layout
from voxcut.ui.server import STATIC, STATIC_DIR, make_server

FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
             "new Function", 'setAttribute("style"', "setAttribute('style'", "localStorage",
             "sessionStorage", "document.cookie", "http://", "https://", "localhost"]
BASECOAT_SHA256 = "744bb04803c3480d1e79b3cfd38ecb6b103075400774cb89bf57b5e27e6d15a2"
LUCIDE_SHA256 = "eeeb8ec76ade50ef1e41547941836815006d4976366b33b7e1ab796ea38ccf24"  # lucide-static 1.52.0, D55/D58
LUCIDE_ICONS = ["play", "pause", "sun-moon", "keyboard", "library", "check", "mic-vocal", "music",
                "audio-waveform", "ellipsis", "notebook-pen", "lightbulb", "pencil", "folder-open",
                "folder-input", "cog", "archive", "x", "maximize-2"]  # D55, D58
LICENSES = {"vendor/LICENSE.md", "vendor/LICENSE-lucide.txt"}


SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'  # a standalone SVG needs it; not a request


def static_files():  # our own files at any depth; vendor/ is pinned by sha256 instead
    return sorted(p for p in STATIC_DIR.rglob("*")
                  if p.is_file() and p.relative_to(STATIC_DIR).parts[0] != "vendor")


@pytest.mark.parametrize("path", static_files(), ids=lambda p: p.relative_to(STATIC_DIR).as_posix())
def test_static_rules(path):
    text = path.read_text()
    if path.suffix == ".svg":
        text = text.replace(SVG_NS, "")
    found = [w for w in FORBIDDEN if w in text]
    assert found == [], f"{path.name}: {found}"
    if path.suffix == ".html":
        assert not re.search(r"\son[a-z]+\s*=", text, re.IGNORECASE), "inline event handler"
        assert not re.search(r"\sstyle\s*=", text, re.IGNORECASE), "inline style"
        assert not re.search(r"<script[^>]*>\s*[^<\s]", text, re.IGNORECASE), "inline script body"
        assert re.findall(r'<script[^>]*src="([^"]+)"', text) == ["/i18n/ru.js", "/app.js"]
        assert re.findall(r'<link[^>]*href="([^"]+)"', text) == [
            "/vendor/basecoat-luma.css", "/app.css", "/favicon.svg"]


def test_light_tokens_scoped():
    # Audit 2026-10-22: light-only overrides; on plain :root they would also win over Basecoat's .dark
    css = (STATIC_DIR / "app.css").read_text()
    blocks = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    for token in ("--muted-foreground:", "--destructive:"):
        where = [sel.strip().split("\n")[-1].strip() for sel, body in blocks if token in body]
        assert where == [":root:not(.dark)"], (token, where)


def test_static_table_covers_all_files():
    files = {p.relative_to(STATIC_DIR).as_posix() for p in STATIC_DIR.rglob("*") if p.is_file()}
    assert {name for name, _ in STATIC.values()} == files - LICENSES


def test_vendor_basecoat_pinned():
    data = (STATIC_DIR / "vendor" / "basecoat-luma.css").read_bytes()
    assert hashlib.sha256(data).hexdigest() == BASECOAT_SHA256
    assert (STATIC_DIR / "vendor" / "LICENSE.md").is_file()


def test_vendor_lucide_pinned():
    data = (STATIC_DIR / "vendor" / "lucide-sprite.svg").read_bytes()
    assert hashlib.sha256(data).hexdigest() == LUCIDE_SHA256
    assert (STATIC_DIR / "vendor" / "LICENSE-lucide.txt").is_file()
    assert re.findall(r'<symbol id="([^"]+)"', data.decode()) == LUCIDE_ICONS


def test_icon_refs_exist_in_sprite():
    refs = set()
    for name in ("index.html", "app.js"):
        refs |= set(re.findall(r"/vendor/lucide-sprite\.svg#([a-z0-9-]+)",
                               (STATIC_DIR / name).read_text()))
    assert refs and refs <= set(LUCIDE_ICONS)


def test_index_served_with_csp(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    server, app = make_server(lay)
    app.log = lambda line: None
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    try:
        c = Client(app)
        assert c.req("GET", "/i18n/ru.js")[1].get("Content-Type") == "text/javascript; charset=utf-8"
        for path in ("/", "/app.js", "/i18n/ru.js", "/app.css", "/vendor/basecoat-luma.css",
                     "/vendor/lucide-sprite.svg", "/favicon.svg"):
            status, headers, _ = c.req("GET", path)
            assert status == 200 and {k: headers.get(k) for k in SECURITY} == SECURITY
        assert c.req("GET", "/favicon.svg")[1].get("Content-Type") == "image/svg+xml"  # D60
    finally:
        server.shutdown()
        server.server_close()


def test_no_translate_prompt():
    text = (STATIC_DIR / "index.html").read_text()
    assert '<html lang="ru" translate="no">' in text
    assert '<meta name="google" content="notranslate">' in text


def test_package_data_covers_static():
    """A wheel ships every file the server serves: `static/*` alone skips vendor/ and img/."""
    import glob
    import tomllib
    from pathlib import Path

    root = Path(__file__).parent.parent
    patterns = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"]["voxcut.ui"]
    pkg = STATIC_DIR.parent
    shipped = {Path(f).resolve() for pat in patterns
               for f in glob.glob(str(pkg / pat), recursive=True) if Path(f).is_file()}
    served = {(STATIC_DIR / name).resolve() for name, _ in STATIC.values()}
    assert served <= shipped, sorted(str(p) for p in served - shipped)
