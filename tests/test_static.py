import hashlib
import json
import re
import threading
from html.parser import HTMLParser

import pytest
from test_server_security import SECURITY, Client

from voxcut.library import Layout
from voxcut.ui.server import STATIC, STATIC_DIR, make_server

FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
             "new Function", 'setAttribute("style"', "setAttribute('style'", "localStorage",
             "sessionStorage", "document.cookie", "http://", "https://", "localhost",
             "navigator.language", "Accept-Language"]  # D69: the UI language is the user's choice only
BASECOAT_SHA256 = "744bb04803c3480d1e79b3cfd38ecb6b103075400774cb89bf57b5e27e6d15a2"
LUCIDE_SHA256 = "eeeb8ec76ade50ef1e41547941836815006d4976366b33b7e1ab796ea38ccf24"  # lucide-static 1.52.0, D55/D58
LUCIDE_ICONS = ["play", "pause", "sun-moon", "keyboard", "library", "check", "mic-vocal", "music",
                "audio-waveform", "ellipsis", "notebook-pen", "lightbulb", "pencil", "folder-open",
                "folder-input", "cog", "archive", "x", "maximize-2"]  # D55, D58
MULISH_SHA256 = "00f1105796291a2fdda117a0fc7f25d8e68f8010cdbb34a411f60b3bd57717ac"  # google/fonts e7db6ca, D77
LICENSES = {"vendor/LICENSE.md", "vendor/LICENSE-lucide.txt", "vendor/OFL-mulish.txt"}


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
        assert re.findall(r'<script[^>]*src="([^"]+)"', text) == [
            "/i18n/en.js", "/i18n/ru.js", "/i18n.js", "/app.js"]
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


def test_vendor_mulish_pinned():
    data = (STATIC_DIR / "vendor" / "Mulish.ttf").read_bytes()
    assert hashlib.sha256(data).hexdigest() == MULISH_SHA256
    assert (STATIC_DIR / "vendor" / "OFL-mulish.txt").is_file()


def test_font_face():
    # D77: Mulish for the whole UI, text visible at once if the file fails; mono places stay system
    css = (STATIC_DIR / "app.css").read_text()
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    assert len(faces) == 1
    for decl in ('font-family: "Mulish"', 'src: url("/vendor/Mulish.ttf") format("truetype")',
                 "font-weight: 200 1000", "font-display: swap"):
        assert decl in faces[0], decl
    assert '--font-sans: "Mulish", system-ui, "Segoe UI", sans-serif' in css
    assert "--font-mono" not in css


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
        for path in ("/", "/app.js", "/i18n.js", "/i18n/en.js", "/i18n/ru.js", "/app.css", "/vendor/basecoat-luma.css",
                     "/vendor/lucide-sprite.svg", "/vendor/Mulish.ttf", "/favicon.svg"):
            status, headers, _ = c.req("GET", path)
            assert status == 200 and {k: headers.get(k) for k in SECURITY} == SECURITY
        assert c.req("GET", "/favicon.svg")[1].get("Content-Type") == "image/svg+xml"  # D60
        assert c.req("GET", "/vendor/Mulish.ttf")[1].get("Content-Type") == "font/ttf"  # D77
    finally:
        server.shutdown()
        server.server_close()


def test_no_translate_prompt():
    text = (STATIC_DIR / "index.html").read_text()
    assert '<html lang="en" translate="no">' in text
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


I18N_ATTRS = {"data-i18n-placeholder": "placeholder", "data-i18n-aria-label": "aria-label",
              "data-i18n-title": "title"}


def en_catalog() -> dict:
    text = (STATIC_DIR / "i18n" / "en.js").read_text()
    return {m[1]: json.loads(m[2]) for m in re.finditer(r'^  "([^"]+)": ("(?:[^"\\]|\\.)*"),?$', text, re.MULTILINE)}


class _I18nText(HTMLParser):
    """(key, text inside the element) for data-i18n, (key, attribute value) for data-i18n-*."""

    def __init__(self):
        super().__init__()
        self.found, self.open = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.found += [(a[k], a.get(attr)) for k, attr in I18N_ATTRS.items() if k in a]
        if "data-i18n" in a:
            self.open.append([a["data-i18n"], ""])

    def handle_data(self, data):
        for item in self.open:
            item[1] += data

    def handle_endtag(self, tag):
        if self.open:  # a data-i18n element holds text only: the next end tag is its own
            self.found.append(tuple(self.open.pop()))


def test_html_text_is_en_catalog():
    """D67, L11: index.html is the English UI, word for word what applyStatic() writes."""
    parser = _I18nText()
    parser.feed((STATIC_DIR / "index.html").read_text())
    en = en_catalog()
    assert len(parser.found) > 40
    assert [(k, v) for k, v in parser.found if en.get(k) != v] == []


DATA_VALUES = ['"моё"', '"вокал"', '"музыка"', '"шум"', '"другое"', '"ё"', '"е"']  # journal data, norm (L14, L15)


@pytest.mark.parametrize("name", ["index.html", "app.js", "i18n.js"])
def test_ui_has_no_cyrillic_outside_catalogs(name):
    """D67: every UI phrase comes from i18n/en.js or i18n/ru.js."""
    text = (STATIC_DIR / name).read_text()
    text = re.sub(r"<!--.*?-->|/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    for value in DATA_VALUES:
        text = text.replace(value, "")
    assert re.findall(r".*[а-яёА-ЯЁ].*", text) == []


def test_library_columns():
    """D78: a wide column (Sessions, Job log) and a narrow one (Inbox, Backup, Ideas); one column keeps this order."""
    html = (STATIC_DIR / "index.html").read_text()
    start = html.index('<main id="screen-library"')
    lib = html[start:html.index("</main>", start)]
    tops = re.findall(r'^  <(\w+)[^>]*class="([^"]+)"', lib, re.MULTILINE)
    assert [cls for _, cls in tops] == ["lib-main", "lib-side"]
    ids = re.findall(r'id="(sessions|jobs|inbox|backup-info|ideas)"', lib)
    assert ids == ["sessions", "jobs", "inbox", "backup-info", "ideas"]
    side = lib.index('class="lib-side"')
    assert lib.index('id="jobs"') < side < lib.index('id="inbox"')


def test_decision_pressed_rule_scoped():
    """D98: the dark fill of a pressed decision is scoped to #decisions; other pressed buttons keep --secondary."""
    css = (STATIC_DIR / "app.css").read_text()
    rules = re.findall(r"([^{}]+)\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    primary = [sel.strip() for sel, body in rules if "aria-pressed=true" in sel and "--primary" in body]
    assert primary and all(s.startswith("#decisions") for s in primary)
    assert any(sel.strip().startswith(".btn[aria-pressed=true]") and "--secondary" in body for sel, body in rules)

