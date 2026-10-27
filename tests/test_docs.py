"""Public documents (D119, D129): links resolve, language pairs point at each other."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")])
LINK = re.compile(r"\]\(([^)\s]+)\)")


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


@pytest.mark.parametrize("doc", DOCS, ids=rel)
def test_relative_links_resolve(doc):
    missing = []
    for target in LINK.findall(doc.read_text()):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        path = target.split("#")[0]
        if path and not (doc.parent / path).exists():
            missing.append(target)
    assert missing == []


@pytest.mark.parametrize("ru", [d for d in DOCS if d.name.endswith(".ru.md")], ids=rel)
def test_language_pair_switch(ru):
    en = ru.with_name(ru.name.removesuffix(".ru.md") + ".md")
    assert en.exists()
    for doc, other in ((en, ru), (ru, en)):
        first = doc.read_text().splitlines()[0]
        assert "English" in first and "Русский" in first and f"]({other.name})" in first


def test_claude_settings_not_tracked():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", ".claude"],
                             capture_output=True, text=True, check=True).stdout
    assert tracked == ""


def test_license_and_notices():
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    for name in ("Silero", "Basecoat", "Lucide", "Mulish"):
        assert name in notices
