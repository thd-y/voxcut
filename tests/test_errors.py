import ast
import re
from pathlib import Path

import pytest

from voxcut.errors import _FIELD as FIELD
from voxcut.errors import CODES, Coded, descriptor, render


@pytest.mark.parametrize("code", sorted(CODES))
def test_templates_match_params(code):
    names, template = CODES[code]
    fields = set(FIELD.findall(template))
    assert "{{" not in template and "}}" not in template  # render() has no escapes
    assert fields == set(names)


def test_render_rules(monkeypatch):
    monkeypatch.setitem(CODES, "t_list", (("files",), "нет: {files}"))
    monkeypatch.setitem(CODES, "t_lines", (("lines",), "цепочки:\n{lines}"))
    monkeypatch.setitem(CODES, "t_cause", (("cause",), "после: {cause}"))
    assert render("t_list", {"files": ["a.wav", "b.wav"]}) == "нет: a.wav, b.wav"
    assert render("t_lines", {"lines": ["  1: a", "  2: b"]}) == "цепочки:\n  1: a\n  2: b"
    cause = {"code": "t_list", "params": {"files": ["x"]}}
    assert render("t_cause", {"cause": cause}) == "после: нет: x"
    assert render("internal_error", {}) == "внутренняя ошибка сервера"


class E(Coded, ValueError):
    pass


def test_coded_rejects_unknown_code_and_params():
    with pytest.raises(KeyError):
        E("no_such_code")
    with pytest.raises(TypeError):
        E("legacy_error")
    with pytest.raises(TypeError):
        E("legacy_error", detail="x", extra=1)


def test_coded_is_its_base():
    with pytest.raises(ValueError) as ei:
        raise E("legacy_error", detail="x")
    assert str(ei.value) == "x"
    assert (ei.value.code, ei.value.params) == ("legacy_error", {"detail": "x"})
    assert descriptor(ei.value) == {"code": "legacy_error", "params": {"detail": "x"}}


def test_descriptor_plain_exception():
    assert descriptor(KeyError("p")) == {"code": "internal_error", "params": {}}


UI_MODULES = ["intervals", "audio", "cli", "library", "journal", "jobs", "ui/server"]
EXEMPT = {"LabelsError", "ConfigError", "SystemExit"}
INTERNAL = {("audio", "RuntimeError"),  # an invariant breach: internal_error in the UI
            ("journal", "ValueError")}  # a bad line's reason, carried as detail of a warning
CYR = re.compile("[а-яёА-ЯЁ]")


def _russian_literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(CYR.search(node.value))
    if isinstance(node, ast.JoinedStr):
        return any(_russian_literal(v) for v in node.values)
    return False


def russian_raises() -> list[str]:
    """`raise X("русский …")` in modules whose errors reach the UI, plus module-level Russian
    string constants in jobs.py (DEAD is written into status.json)."""
    root = Path(__file__).parent.parent / "voxcut"
    found = []
    for mod in UI_MODULES:
        path = root / f"{mod}.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and getattr(node.exc.func, "id", getattr(node.exc.func, "attr", None))
                    not in EXEMPT
                    and (mod, getattr(node.exc.func, "id", None)) not in INTERNAL
                    and any(_russian_literal(x) for a in [*node.exc.args,
                                                         *(k.value for k in node.exc.keywords)]
                            for x in ast.walk(a))):
                found.append(f"{mod}.py:{node.lineno}")
        if mod == "jobs":
            found += [f"{mod}.py:{n.lineno}" for n in tree.body
                      if isinstance(n, ast.Assign) and _russian_literal(n.value)]
    return found


@pytest.mark.xfail(strict=True, reason="цикл 1, слой 2–3")
def test_no_russian_raise_literals():
    assert russian_raises() == []


RU_TEXT = [
    ("session_overlap", {"a": "1.wav", "b": "2.wav", "gap": "0.50", "tol": "0.1"},
     "файлы 1.wav и 2.wav перекрываются по времени на 0.50 с (допуск 0.1 с)"),
    ("session_pause", {"minutes": "3.0", "a": "1.wav", "b": "2.wav"},
     "между частями пауза 3.0 мин (1.wav → 2.wav); склейка пауз пока не поддерживается"),
    ("session_chain_range", {"n": 3, "total": 2, "lines": ["  1: a.wav, b.wav", "  2: c.wav"]},
     "--chain 3 вне диапазона 1..2:\n  1: a.wav, b.wav\n  2: c.wav"),
    ("source_not_riff", {"src": "/x/a.wav"}, "/x/a.wav: не RIFF/RF64 WAVE"),
    ("source_codec", {"src": "a.wav", "codec": "adpcm_ima_wav", "fix": "ffmpeg -i a.wav b.flac"},
     ("a.wav: кодек adpcm_ima_wav, сэмпл-точность не гарантирована.\n"
     "Конвертируйте: ffmpeg -i a.wav b.flac\nдальше источник — этот файл")),
    ("import_move_stuck", {"file": "b.WAV", "detail": "[Errno 18] x", "files": ["a.WAV", "b.WAV"],
                           "dir": "/d/originals.tmp"},
     ("b.WAV: перенос не удался ([Errno 18] x); вернуть не удалось: a.WAV, b.WAV — они в "
     "/d/originals.tmp, не удаляйте их: это единственные копии; остальные файлы во входящих")),
    ("import_unfinished_elsewhere", {"session": "s1"},
     ("часть файлов уже в сессии s1 (незавершённый импорт): нажмите у неё «Обработать»; "
     "импорт отменён, файлы на месте")),
    ("backup_full_refused", {"files": ["sessions/s/originals.tmp/a.WAV"]},
     ("full-бэкап отменён: записи вне originals/ или ссылки — sessions/s/originals.tmp/a.WAV "
     "(originals.tmp — незавершённый импорт, обычно его доделывает «Обработать»)")),
    ("restore_conflicts", {"lines": ["  data/a", "  data/b"]},
     "в data/ уже есть другие версии файлов, ничего не изменено:\n  data/a\n  data/b"),
    ("run_no_reset_period", {"dir": "/r/p", "rm": "/r/p"},
     ("/r/p: probs посчитаны без второго прохода Silero (D31).\n"
     "Удалите каталог и запустите заново: rm -rf /r/p")),
]


@pytest.mark.parametrize(("code", "params", "text"), RU_TEXT)
def test_ru_text_unchanged(code, params, text):
    assert str(E(code, **params)) == text
