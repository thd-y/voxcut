import json
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
    assert render("internal_error", {}) == "internal server error"


class E(Coded, ValueError):
    pass


def test_coded_rejects_unknown_code_and_params():
    with pytest.raises(KeyError):
        E("no_such_code")
    with pytest.raises(TypeError):
        E("legacy_error")
    with pytest.raises(TypeError):
        E("legacy_error", detail="x", extra=1)


@pytest.mark.parametrize("value", [None, Path("/x"), True, b"x"])
def test_coded_rejects_param_types(value):  # a Path would kill json.dumps in jobs._write; None renders "None"/"null"
    with pytest.raises(TypeError):
        E("source_no_audio", src=value)


def test_coded_rejects_list_items():
    with pytest.raises(TypeError):
        E("session_foreign_names", dir="d", files=["a", Path("b")])


@pytest.mark.parametrize(("code", "params"), [
    ("source_no_audio", {"src": "x"}),
    ("journal_value_too_long", {"limit": 3}),
    ("session_overlap", {"a": "1", "b": "2", "gap": 0.5, "tol": 0.1}),
    ("session_foreign_names", {"dir": "d", "files": ["a"]}),
    ("legacy_error", {"detail": {"code": "internal_error", "params": {}}}),
])
def test_coded_accepts_param_types(code, params):
    assert E(code, **params).params == params


def test_coded_is_its_base():
    with pytest.raises(ValueError) as ei:
        raise E("legacy_error", detail="x")
    assert str(ei.value) == "x"
    assert (ei.value.code, ei.value.params) == ("legacy_error", {"detail": "x"})
    assert descriptor(ei.value) == {"code": "legacy_error", "params": {"detail": "x"}}


def test_descriptor_plain_exception():
    assert descriptor(KeyError("p")) == {"code": "internal_error", "params": {}}


def test_no_cyrillic_in_backend_strings():  # D114: comments may stay Russian, output may not
    import tokenize

    found = []
    for path in sorted((Path(__file__).parent.parent / "voxcut").rglob("*.py")):
        with open(path, encoding="utf-8") as f:
            found += [f"{path.name}:{t.start[0]}" for t in tokenize.generate_tokens(f.readline)
                      if t.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE)
                      and re.search("[а-яёА-ЯЁ]", t.string)]
    assert found == []


EN_TEXT = [
    ("session_overlap", {"a": "1.wav", "b": "2.wav", "gap": "0.50", "tol": "0.1"},
     "files 1.wav and 2.wav overlap in time by 0.50 s (tolerance 0.1 s)"),
    ("session_pause", {"minutes": "3.0", "a": "1.wav", "b": "2.wav"},
     "a 3.0 min pause between parts (1.wav → 2.wav); joining across pauses is not supported yet"),
    ("session_chain_range", {"n": 3, "total": 2, "lines": ["  1: a.wav, b.wav", "  2: c.wav"]},
     "--chain 3 is out of range 1..2:\n  1: a.wav, b.wav\n  2: c.wav"),
    ("source_not_riff", {"src": "/x/a.wav"}, "/x/a.wav: not a RIFF/RF64 WAVE"),
    ("source_codec", {"src": "a.wav", "codec": "adpcm_ima_wav", "fix": "ffmpeg -i a.wav b.flac"},
     ("a.wav: codec adpcm_ima_wav, sample-accurate decoding is not guaranteed.\n"
     "Convert it: ffmpeg -i a.wav b.flac\nthen use that file as the source")),
    ("import_move_stuck", {"file": "b.WAV", "detail": "[Errno 18] x", "files": ["a.WAV", "b.WAV"],
                           "dir": "/d/originals.tmp"},
     ("b.WAV: move failed ([Errno 18] x); could not move back: a.WAV, b.WAV; they are in "
     "/d/originals.tmp. Do not delete them: they are the only copies; the other files are in the inbox")),
    ("import_unfinished_elsewhere", {"session": "s1"},
     ("some files are already in session s1 (unfinished import): press “Process” on it; "
     "import cancelled, files left in place")),
    ("backup_full_refused", {"files": ["sessions/s/originals.tmp/a.WAV"]},
     ("full backup cancelled: entries outside originals/ or links: sessions/s/originals.tmp/a.WAV "
     "(originals.tmp is an unfinished import, “Process” usually finishes it)")),
    ("restore_conflicts", {"lines": ["  data/a", "  data/b"]},
     "data/ already has other versions of the files, nothing changed:\n  data/a\n  data/b"),
    ("journal_value_too_long", {"limit": 500}, "text longer than 500 characters"),  # D73
    ("http_no_code", {}, "no launch code"),  # D72
    ("run_no_reset_period", {"dir": "/r/p", "rm": "/r/p"},
     ("/r/p: probs were computed without Silero's second pass (D31).\n"
     "Delete the folder and run again: rm -rf /r/p")),
]


@pytest.mark.parametrize(("code", "params", "text"), EN_TEXT)
def test_en_text(code, params, text):
    assert str(E(code, **params)) == text


RU_JS = Path(__file__).parent.parent / "voxcut" / "ui" / "static" / "i18n" / "ru.js"
FRONT_ONLY = {"unknown", "offline", "not_saved", "http_status"}
EN_JS = RU_JS.with_name("en.js")


def _catalog(path):
    return {m[1]: json.loads(m[2])
            for m in re.finditer(r'"err\.(\w+)":\s*("(?:[^"\\]|\\.)*")', path.read_text())}


def test_en_catalog_matches_registry():
    """D66, D114: the registry's templates are en.js's, byte for byte."""
    found = _catalog(EN_JS)
    assert set(found) == set(CODES) | FRONT_ONLY
    assert {c: found[c] for c in CODES} == {c: t for c, (_, t) in CODES.items()}


def test_ru_catalog_covers_registry():
    """D67: every registry code has a Russian template with the registry's parameters."""
    found = _catalog(RU_JS)
    assert set(found) == set(CODES) | FRONT_ONLY
    assert {c: set(FIELD.findall(found[c])) for c in CODES} == {c: set(n) for c, (n, _) in CODES.items()}
