"""Public documents (D119, D129): links resolve, language pairs point at each other."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")])
LINK = re.compile(r"\]\(([^)\s]+)\)")
# A commit link from docs/ climbs out of blob/main/docs to the repository root on GitHub.
COMMIT = re.compile(r"^\.\./\.\./\.\./commit/([0-9a-f]{40})$")


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


@pytest.mark.parametrize("doc", DOCS, ids=rel)
def test_relative_links_resolve(doc):
    missing = []
    for target in LINK.findall(doc.read_text()):
        if re.match(r"[a-z]+:", target) or target.startswith("#") or COMMIT.match(target):
            continue
        path = target.split("#")[0]
        if path and not (doc.parent / path).exists():
            missing.append(target)
    assert missing == []


@pytest.mark.parametrize("doc", DOCS, ids=rel)
def test_commit_links_exist(doc):
    for target in LINK.findall(doc.read_text()):
        if m := COMMIT.match(target):
            assert doc.parent.name == "docs", target
            cmd = ["git", "-C", str(ROOT), "cat-file", "-e", m[1] + "^{commit}"]
            assert subprocess.run(cmd, check=False).returncode == 0, target


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


def decision_numbers(text: str) -> list[str]:
    """Numbers an entry defines: `**D12 — `, `**C3 — `, or a range line `**D118–D127, D132–D133: `."""
    out = re.findall(r"^\*\*([DC]\d+) — ", text, re.MULTILINE)
    for line in re.findall(r"^\*\*(D\d+–D\d+(?:, D\d+–D\d+)*): ", text, re.MULTILINE):
        for a, b in re.findall(r"D(\d+)–D(\d+)", line):
            out += [f"D{n}" for n in range(int(a), int(b) + 1)]
    return out


def test_decision_numbers_parse_singles_and_ranges():
    text = "**D1 — A.** x\n**C2 — B.** y\n**D3–D5, D7–D8: z.** w\nsee D9\n"
    assert decision_numbers(text) == ["D1", "C2", "D3", "D4", "D5", "D7", "D8"]


def test_decision_log_covers_every_number_once():
    got = decision_numbers((ROOT / "docs" / "decisions.md").read_text())
    want = [f"D{n}" for n in range(1, 134)] + [f"C{n}" for n in range(1, 8)]
    assert sorted(got) == sorted(want)
