"""Append-only journal of marks, notes, ideas (U5). The server is the only writer."""

import fcntl
import json
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from voxcut.intervals import intersect, total, union

KINDS = ("mark", "note", "idea", "heard", "backup")
MARK_MAX = 32
NOTE_MAX = 10_000
SPAN_KINDS = ("mark", "note", "heard")  # bound to a moment of the recording
EVENT_ID_RE = re.compile(r"[0-9a-f]{32}")
FIELDS = {"event_id", "kind", "value", "session_id", "start_s", "end_s"}


class JournalError(ValueError):
    pass


def _parse(data: bytes, where: str) -> tuple[list[dict], list[str], int]:
    """Events, warnings and the byte length of the good prefix.

    A line counts only with its trailing newline: append writes line+newline in one write and
    acknowledges after fsync, so anything after the last newline was never acknowledged.
    """
    events, pos = [], 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        try:
            if nl < 0:
                raise ValueError("нет конца строки")
            e = json.loads(data[pos:nl])
            if not isinstance(e, dict) or not isinstance(e.get("event_id"), str):
                raise ValueError("не событие")  # noqa: TRY004 - one except path for all bad lines
        except ValueError as exc:
            if nl >= 0 and nl + 1 < len(data):
                raise JournalError(f"{where}: битая строка в середине журнала (байт {pos})") from exc
            return events, [f"{where}: последняя строка журнала битая и пропущена ({exc})"], pos
        events.append(e)
        pos = nl + 1
    return events, [], pos


def load(path: Path) -> tuple[list[dict], list[str]]:
    events, warnings, _ = _parse(Path(path).read_bytes(), str(path))
    return events, warnings


def _validate(event: dict) -> dict:
    if not isinstance(event, dict) or set(event) - FIELDS:
        raise JournalError("неизвестные поля события")
    e = dict(event)
    if not isinstance(e.get("event_id"), str) or not EVENT_ID_RE.fullmatch(e["event_id"]):
        raise JournalError("event_id: нужны 32 hex")
    if e.get("kind") not in KINDS:
        raise JournalError("неизвестный вид события")
    limit = MARK_MAX if e["kind"] == "mark" else NOTE_MAX
    if not isinstance(e.get("value"), str) or len(e["value"]) > limit:
        raise JournalError(f"value: строка не длиннее {limit}")
    sid = e.get("session_id")
    if sid is not None and (not isinstance(sid, str) or not sid or len(sid) > 200):
        raise JournalError("session_id")
    span = [e.get(k) for k in ("start_s", "end_s")]
    if e["kind"] in SPAN_KINDS:
        ok = sid is not None and all(
            isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)
            for x in span
        )
        if not ok or not 0 <= span[0] < span[1]:
            raise JournalError("отрезок: нужны session_id и 0 <= start_s < end_s")
    elif span != [None, None] or (e["kind"] == "backup" and sid is not None):
        raise JournalError("у этого вида события нет отрезка")
    return e


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._ids: dict[str, dict] = {}
        self._good = 0
        self.warnings: list[str] = []
        self.events()

    def _catch_up(self, fd: int) -> None:
        """Read what other writers (a second server) appended since our last look."""
        size = os.fstat(fd).st_size
        data = os.pread(fd, size - self._good, self._good) if size > self._good else b""
        events, self.warnings, good = _parse(data, str(self.path))
        for e in events:
            self._ids.setdefault(e["event_id"], e)
        self._events += events
        self._good += good

    def events(self) -> list[dict]:
        with self._lock:
            if self.path.exists():
                fd = os.open(self.path, os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH)
                    self._catch_up(fd)
                finally:
                    os.close(fd)
            return list(self._events)

    def append(self, event: dict) -> dict:
        e = _validate(event)
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._catch_up(fd)
                if e["event_id"] in self._ids:
                    return dict(self._ids[e["event_id"]])
                self._set_aside_torn_tail(fd)
                e["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
                line = (json.dumps(e, ensure_ascii=False) + "\n").encode()
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            self._good += len(line)
            self._events.append(e)
            self._ids[e["event_id"]] = e
            return dict(e)

    def _set_aside_torn_tail(self, fd: int) -> None:
        """A torn last line would become a broken middle line: move its bytes to <journal>.torn."""
        size = os.fstat(fd).st_size
        if size == self._good:
            return
        tail = os.pread(fd, size - self._good, self._good)
        with open(self.path.with_name(self.path.name + ".torn"), "ab") as f:
            f.write(tail + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.ftruncate(fd, self._good)
        os.fsync(fd)
        self.warnings = []


def clip_view(events: list[dict], session_id: str, start_s: float, end_s: float) -> dict:
    last: dict[tuple, dict] = {}
    heard = []
    for e in events:
        if e.get("session_id") != session_id or e.get("kind") not in SPAN_KINDS:
            continue
        if not (e["start_s"] < end_s and e["end_s"] > start_s):
            continue
        if e["kind"] == "heard":
            heard.append((e["start_s"], e["end_s"]))
        else:
            last[(e["start_s"], e["end_s"], e["kind"])] = e
    marks = [e["value"] for k, e in last.items() if k[2] == "mark" and e["value"]]
    notes = [
        {"event_id": e["event_id"], "value": e["value"]}
        for k, e in last.items()
        if k[2] == "note" and e["value"]
    ]
    covered = total(intersect(union(heard), [(start_s, end_s)]))
    return {
        "marks": list(dict.fromkeys(marks)),
        "notes": notes,
        "heard": covered >= 0.5 * (end_s - start_s),
    }


def ideas(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "idea"]


def last_backup(events: list[dict]) -> str | None:
    return next((e["ts"] for e in reversed(events) if e.get("kind") == "backup"), None)
