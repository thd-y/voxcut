"""Job log and CLI stderr line format (D115): `DD.MM.YY HH:MM LEVEL message`. Pure, no IO."""

from datetime import datetime


def line(level: str, msg: str, when: datetime) -> str:
    # Continuation lines of a multi-line msg stay as they are, without the prefix.
    return f"{when:%d.%m.%y %H:%M} {level:<5} {msg}"


def duration(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s" if m else f"{s}s"
