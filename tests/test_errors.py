import string

import pytest

from voxcut.errors import CODES, Coded, descriptor, render


@pytest.mark.parametrize("code", sorted(CODES))
def test_templates_match_params(code):
    names, template = CODES[code]
    fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
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
