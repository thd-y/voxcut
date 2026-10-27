import json
import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

from tools import leakcheck

VALUE = "SECRET-VALUE-1"
WORD = "own" "er"  # split, or this file trips its own style rule
# D121: cut from the public history; they carry Russian prose by design
PROCESS_DOCS = ("docs/", "CLAUDE.md", "TODOS.md", "PRODUCT.md", "DESIGN.md", "README.md",
                ".claude/", ".impeccable/")


def git(repo, *args, stdin=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, input=stdin).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "start")
    values = tmp_path / "values.txt"
    values.write_text(f"# characteristic values\n\n{VALUE}\n")
    monkeypatch.chdir(r)
    monkeypatch.delenv("VOXCUT_LEAK_VALUES", raising=False)
    return r, values


def stage(r, path, text):
    p = r / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())
    git(r, "add", path)


def run(values, *args):
    return leakcheck.main([*args, "--values", str(values)])


@pytest.mark.parametrize("path, text, refused", [
    ("b.py", f"x = '{VALUE.lower()}'\n", True),  # values match in any case
    (f"dir-{VALUE}/b.py", "x = 2\n", True),  # value in a path
    ("b.bin", b"\x00\x01" + VALUE.encode() + b"\x00", True),  # value in a binary file
    ("b.py", ("p = '/home" "/alice/x'\n"), True),
    ("b.py", ("p = '/mnt/c" "/Users/alice/x'\n"), True),
    ("b.py", "p = '/home/user/x'\n", False),
    ("README.md", "# Заголовок\n", True),
    ("b.py", 'x = "текст"\n', True),
    ("b.py", "x = 1  # комментарий\n", False),
    ("b.js", "x = 1;  // комментарий\n", False),
    ("README.ru.md", "Текст\n", False),
    ("tests/x.py", 'x = "текст"\n', False),
    ("voxcut/ui/static/i18n/ru.js", '"k": "текст",\n', False),
    ("voxcut/ui/static/app.js", 'const KEEP = "моё";\n', False),  # tools/leak-allow.txt
    ("b.py", f"# the {WORD} said\n", True),
    ("b.py", "x = 'user'\n", False),
    ("docs/superpowers/plans/x.md", "plan\n", True),
    ("docs/decisions.md", "decisions\n", False),
    ("a.WAV", b"RIFF", True),
])
def test_staged(repo, path, text, refused):
    r, values = repo
    stage(r, path, text)
    assert run(values, "staged") == int(refused)


def test_hit_names_rule_not_value(repo, capsys):
    r, values = repo
    stage(r, "b.py", f"\n\nx = '{VALUE}'\n")
    assert run(values, "staged") == 1
    out = capsys.readouterr()
    assert "b.py:3: value#1" in out.out + out.err
    assert VALUE not in out.out + out.err


@pytest.mark.parametrize("msg, refused", [
    (f"Fix {VALUE}\n", True),
    (f"Fix the {WORD}'s thing\n", True),
    ("Исправление\n", True),
    ("Fix a thing\n\n# Комментарий git, его нет в коммите\n", False),
])
def test_msg(repo, tmp_path, msg, refused):
    _, values = repo
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(msg)
    assert run(values, "msg", str(f)) == int(refused)


def commit(r, path, text, msg="change"):
    stage(r, path, text)
    git(r, "commit", "-qm", msg)
    return git(r, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("path, text, msg, refused", [
    ("b.py", 'x = "текст"\n', "Исправление", False),  # old history legally has Cyrillic and the word
    ("b.py", f"# the {WORD} said\n", "change", False),
    ("b.py", f"x = '{VALUE}'\n", "change", True),
    ("b.py", "x = 2\n", f"Fix {VALUE}", True),
    ("docs/superpowers/x.md", "plan\n", "change", True),
])
def test_history_and_range(repo, path, text, msg, refused):
    r, values = repo
    base = git(r, "rev-parse", "HEAD").strip()
    commit(r, path, text, msg)
    commit(r, "c.py", "y = 1\n")  # the leak is in an older commit, not at HEAD
    git(r, "rm", "-q", "--cached", path)
    git(r, "commit", "-qm", "drop")
    assert run(values, "history") == int(refused)
    assert run(values, "range", f"{base}..HEAD") == int(refused)
    assert run(values, "range", "HEAD~1..HEAD") == 0  # the leak is outside the range


def test_tree(repo):
    r, values = repo
    assert run(values, "tree") == 0
    (r / "b.py").write_text(f"x = '{VALUE}'\n")
    git(r, "add", "b.py")
    assert run(values, "tree") == 1


def test_values_from_env_and_missing_file(repo, monkeypatch, capsys, tmp_path):
    r, values = repo
    stage(r, "b.py", f"x = '{VALUE}'\n")
    monkeypatch.setenv("VOXCUT_LEAK_VALUES", str(values))
    assert leakcheck.main(["staged"]) == 1
    monkeypatch.setenv("VOXCUT_LEAK_VALUES", str(tmp_path / "none.txt"))
    assert leakcheck.main(["tree"]) == 0  # tree (CI) runs on the general patterns alone
    assert "none.txt" in capsys.readouterr().err


def test_scan_text_interface():
    pats = [re.compile("/home" r"/(?!user/)[^/\s]+/")]
    assert leakcheck.scan_text("a\n/home" "/bob/x\nSeCrEt\n", "f", ["secret"], pats) == [
        ("f", 2, "pattern#1"), ("f", 3, "value#1")]


def test_repo_tree_is_clean_of_style_leaks_outside_process_docs():
    """The real tree minus the process docs (cut from the public history, D121) passes tree's
    style rules; the values are private and not checked here."""
    root = leakcheck.git("rev-parse", "--show-toplevel").strip()
    files = [f for f in leakcheck.git("-C", root, "ls-files").splitlines()
             if not f.startswith(PROCESS_DOCS)]
    hits = [h for f in files for h in leakcheck.scan_file(f, (Path(root) / f).read_bytes(), [],
                                                          leakcheck.load_patterns(), style=True)]
    assert hits == []


def test_history_checks_every_path_of_a_blob(repo):
    """Review C1: one blob, many paths; a renamed or empty file is checked under each path."""
    r, values = repo
    commit(r, f"{VALUE}.txt", "same\n")
    commit(r, "empty.wav", "")
    git(r, "mv", f"{VALUE}.txt", "plain.txt")
    git(r, "rm", "-q", "empty.wav")
    stage(r, "empty.py", "")
    git(r, "commit", "-qm", "rename")
    assert run(values, "history") == 1
    hits = leakcheck.scan("history", [], [VALUE], [])
    assert {(p, rule) for p, _, rule in hits} >= {(f"{VALUE}.txt", "value#1"),
                                                  ("empty.wav", "path:audio")}


def test_range_checks_a_new_path_for_an_old_blob(repo):
    r, values = repo
    base = commit(r, "docs/x.md", "plan\n")
    (r / "docs/superpowers").mkdir()
    git(r, "mv", "docs/x.md", "docs/superpowers/x.md")
    git(r, "commit", "-qm", "move")
    assert run(values, "range", f"{base}..HEAD") == 1


@pytest.mark.parametrize("mode", ["staged", "range"])
def test_hooks_refuse_without_values_file(repo, tmp_path, mode):
    """Review I1: a hook without the private values would pass real leaks."""
    r, _ = repo
    stage(r, "b.py", "x = 1\n")
    args = [mode] + (["HEAD~0..HEAD"] if mode == "range" else [])
    assert leakcheck.main([*args, "--values", str(tmp_path / "none.txt")]) == 2


@pytest.mark.parametrize("text", [
    "cd /home" + "/alice", '"/home' + '/alice"', "\\\\wsl.localhost\\Ubuntu\\home" + "\\alice\\x",
    '"C:\\\\Users' + '\\\\Alice"', "c:\\users" + "\\alice", "/mnt/c/users" + "/alice", "C:/Users" + "/Alice",
])
def test_home_path_forms(text):
    """Review I2: the home of a named user in every common spelling."""
    assert leakcheck.scan_text(text, "f", [], leakcheck.load_patterns())


@pytest.mark.parametrize("text", ["/home/user/x", "/home/user", "/mnt/c/Program Files/x"])
def test_home_path_neutral(text):
    assert not leakcheck.scan_text(text, "f", [], leakcheck.load_patterns())


@pytest.mark.parametrize("text", [
    json.dumps({"n": "Йожин Тест"}),  # \u escapes, the json.dumps default
    unicodedata.normalize("NFD", "Йожин Тест"),
])
def test_value_in_escaped_or_decomposed_form(text):
    """Review I3."""
    assert leakcheck.scan_text(text, "f", ["Йожин Тест"], [])
