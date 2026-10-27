"""Leak check (D128): private values and general patterns in staged files, commit messages,
pushed ranges, the tree and the whole history. A hit prints path:line and the rule number,
never the value itself.

    python -m tools.leakcheck staged|msg <file>|range <revs...>|tree|history [--values FILE]

Values (one per line) come from --values, else $VOXCUT_LEAK_VALUES, else ../leak-values.txt
next to the repository (the private work repo, D127). Style rules (Cyrillic, the word for the
person running the product) run in staged, msg and tree only: old history legally has both.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
CYRILLIC = re.compile("[\\u0400-\\u04ff]")
COMMENT = re.compile(r"#|//|/\*|^\s*\*|^\s*@?(rem\b|::)", re.IGNORECASE)
ROLE_WORD = re.compile(r"\b" + "own" + r"er\b", re.IGNORECASE)
AUDIO = re.compile(r"\.(wav|flac|mp3|m4a|ogg|opus|rf64|npy)$", re.IGNORECASE)  # as in .gitignore
FORBIDDEN = ("docs/superpowers/",)
CYRILLIC_OK = ("tests/", "voxcut/ui/static/i18n/", "tools/leak-allow.txt")


def git(*args: str, data: bytes | None = None) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          input=data).stdout.decode("utf-8", "replace")


def git_bytes(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True, input=data).stdout


def _lines(path: Path) -> list[str]:
    return [s.rstrip("\n") for s in path.read_text().splitlines()
            if s.strip() and not s.lstrip().startswith("#")]


def load_patterns(path: Path = HERE / "leak-patterns.txt") -> list[re.Pattern]:
    return [re.compile(s) for s in _lines(path)]


def load_allow(path: Path = HERE / "leak-allow.txt") -> list[tuple[str, str]]:
    return [tuple(s.split("\t", 1)) for s in _lines(path)]


def scan_text(text: str, path: str, values: list[str], patterns: list[re.Pattern]) -> list:
    """Hits as (path, line_no, rule): rule is pattern#<n> or value#<n>, 1-based."""
    low = [v.lower() for v in values]
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        hits += [(path, n, f"pattern#{i}") for i, p in enumerate(patterns, 1) if p.search(line)]
        hits += [(path, n, f"value#{i}") for i, v in enumerate(low, 1) if v in line.lower()]
    return hits


def style_hits(text: str, path: str, prose: bool) -> list:
    allow = load_allow()
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if ROLE_WORD.search(line):
            hits.append((path, n, "role-word"))
        m = CYRILLIC.search(line)
        if not m or path.startswith(CYRILLIC_OK) or path.endswith(".ru.md"):
            continue
        if any(path == p and frag in line for p, frag in allow):
            continue
        c = None if prose else COMMENT.search(line)
        if not c or c.start() > m.start():
            hits.append((path, n, "cyrillic"))
    return hits


def scan_file(path: str, data: bytes, values: list[str], patterns: list[re.Pattern],
              style: bool = False) -> list:
    hits = [(path, 0, rule) for _, _, rule in scan_text(path, path, values, patterns)]
    if path.startswith(FORBIDDEN):
        hits.append((path, 0, "path:docs/superpowers"))
    if AUDIO.search(path):
        hits.append((path, 0, "path:audio"))
    text = data.decode("utf-8", "replace")  # binary files too: a value is a byte run
    hits += scan_text(text, path, values, patterns)
    if style and b"\0" not in data:
        hits += style_hits(text, path, prose=path.endswith(".md"))
    return hits


def _blobs(revs: list[str]):
    """(path, bytes) for every blob reachable from revs, each object once."""
    named = {}
    for line in git("rev-list", "--objects", *revs).splitlines():
        sha, _, path = line.partition(" ")
        if path:
            named.setdefault(sha, path)
    if not named:
        return
    # shortcut: the whole batch is read into memory; fine for this repo's size, stream if it grows
    out = git_bytes("cat-file", "--batch", data="\n".join(named).encode() + b"\n")
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        sha, kind, size = out[pos:end].decode().split()
        body = out[end + 1:end + 1 + int(size)]
        pos = end + 1 + int(size) + 1
        if kind == "blob":
            yield named[sha], body


def _commits(revs: list[str]):
    """(label, author/committer + message) for every commit in revs."""
    for rec in git("log", "-z", "--format=%H%n%an <%ae>%n%cn <%ce>%n%B", *revs).split("\0"):
        if rec.strip():
            sha, _, rest = rec.partition("\n")
            yield f"commit {sha[:8]} message", rest


def scan(mode: str, rest: list[str], values: list[str], patterns: list[re.Pattern]) -> list:
    hits = []
    if mode == "staged":
        names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").split("\0")
        for p in filter(None, names):
            hits += scan_file(p, git_bytes("show", f":{p}"), values, patterns, style=True)
    elif mode == "msg":
        text = "\n".join(s for s in Path(rest[0]).read_text().splitlines()
                         if not s.startswith("#"))  # git drops these lines
        hits += scan_text(text, "message", values, patterns) + style_hits(text, "message", True)
    elif mode == "tree":
        root = Path(git("rev-parse", "--show-toplevel").strip())
        for p in filter(None, git("ls-files", "-z").split("\0")):
            if (root / p).is_file():
                hits += scan_file(p, (root / p).read_bytes(), values, patterns, style=True)
    elif mode in ("history", "range"):
        revs = ["--all"] if mode == "history" else rest
        for p, data in _blobs(revs):
            hits += scan_file(p, data, values, patterns)
        for label, text in _commits(revs):
            hits += scan_text(text, label, values, patterns)
    else:
        raise SystemExit(f"leakcheck: unknown mode {mode!r}")
    return hits


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    vpath = None
    if "--values" in argv:  # by hand: range takes rev-list args like --not --remotes
        i = argv.index("--values")
        vpath = Path(argv[i + 1])
        del argv[i:i + 2]
    if vpath is None:
        env = os.environ.get("VOXCUT_LEAK_VALUES")
        vpath = Path(env) if env else (
            Path(git("rev-parse", "--show-toplevel").strip()).parent / "leak-values.txt")
    values = []
    if vpath.is_file():
        values = [s.strip() for s in _lines(vpath)]
    else:
        print(f"leakcheck: no values file {vpath}; general patterns only", file=sys.stderr)
    mode, *rest = argv
    hits = scan(mode, rest, values, load_patterns())
    for path, line, rule in hits:
        print(f"{path}:{line}: {rule}", file=sys.stderr)
    if hits:
        print(f"leakcheck: {len(hits)} hit(s); remove the value, do not bypass the check",
              file=sys.stderr)
    return int(bool(hits))


if __name__ == "__main__":
    sys.exit(main())
