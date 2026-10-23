"""Error contract (D66): a machine code plus explicit params; the UI translates, the backend never
knows its language. The Russian template here keeps str(exc) for the CLI and logs as it was."""

import re

# code -> (param names, Russian template). Templates use {name} only; precision is in the value.
CODES: dict[str, tuple[tuple[str, ...], str]] = {
    "internal_error": ((), "внутренняя ошибка сервера"),
    "legacy_error": (("detail",), "{detail}"),
}

_FIELD = re.compile(r"\{(\w+)\}")


def _value(name: str, v) -> str:
    if isinstance(v, dict):
        return render(v["code"], v["params"])
    if isinstance(v, list):
        return ("\n" if name == "lines" else ", ").join(str(x) for x in v)
    return str(v)


def render(code: str, params: dict) -> str:
    return _FIELD.sub(lambda m: _value(m[1], params[m[1]]), CODES[code][1])


class Coded(Exception):
    """First base of a UI-facing exception: class SessionError(Coded, ValueError)."""

    def __init__(self, code: str, **params):
        names = CODES[code][0]
        if set(params) != set(names):
            raise TypeError(f"{code}: params {sorted(params)} != {sorted(names)}")
        self.code, self.params = code, params
        super().__init__(render(code, params))


def descriptor(exc: BaseException) -> dict:
    if isinstance(exc, Coded):
        return {"code": exc.code, "params": exc.params}
    return {"code": "internal_error", "params": {}}
