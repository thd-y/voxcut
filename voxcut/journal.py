"""Append-only journal of marks, notes, ideas (U5). The server is the only writer."""

import fcntl
import json
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from voxcut.errors import Coded
from voxcut.intervals import intersect, total, union

KINDS = ("mark", "note", "idea", "heard", "backup", "idea_deleted")
MARK_MAX = 32
NOTE_MAX = 10_000
SPAN_KINDS = ("mark", "note", "heard")  # bound to a moment of the recording
EVENT_ID_RE = re.compile(r"[0-9a-f]{32}")
FIELDS = {"event_id", "kind", "value", "session_id", "start_s", "end_s"}


class JournalError(Coded, ValueError):
    pass


def _parse(data: bytes, where: str) -> tuple[list[dict], list[dict], int]:
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
                raise JournalError("journal_broken_middle", where=where, pos=pos) from exc
            return events, [{"code": "journal_tail_skipped",
                             "params": {"where": where, "detail": str(exc)}}], pos
        events.append(e)
        pos = nl + 1
    return events, [], pos


def load(path: Path) -> tuple[list[dict], list[dict]]:
    events, warnings, _ = _parse(Path(path).read_bytes(), str(path))
    return events, warnings


def _when(e: dict) -> datetime:
    return datetime.fromisoformat(e["ts"])


def _validate(event: dict, with_ts: bool = False) -> dict:
    if not isinstance(event, dict) or set(event) - FIELDS - ({"ts"} if with_ts else set()):
        raise JournalError("journal_unknown_fields")
    e = dict(event)
    if with_ts:
        try:
            if _when(e).tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise JournalError("journal_bad_ts") from None
    if not isinstance(e.get("event_id"), str) or not EVENT_ID_RE.fullmatch(e["event_id"]):
        raise JournalError("journal_bad_event_id")
    if e.get("kind") not in KINDS:
        raise JournalError("journal_unknown_kind")
    limit = MARK_MAX if e["kind"] == "mark" else NOTE_MAX
    if not isinstance(e.get("value"), str):
        raise JournalError("journal_bad_value")
    if len(e["value"]) > limit:
        raise JournalError("journal_value_too_long", limit=limit)
    sid = e.get("session_id")
    if sid is not None and (not isinstance(sid, str) or not sid or len(sid) > 200):
        raise JournalError("journal_bad_session_id")
    span = [e.get(k) for k in ("start_s", "end_s")]
    if e["kind"] in SPAN_KINDS:
        ok = sid is not None and all(
            isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)
            for x in span
        )
        if not ok or not 0 <= span[0] < span[1]:
            raise JournalError("journal_bad_span")
    elif span != [None, None] or (e["kind"] in ("backup", "idea_deleted") and sid is not None):
        raise JournalError("journal_no_span")
    if e["kind"] == "idea_deleted" and not EVENT_ID_RE.fullmatch(e["value"]):
        raise JournalError("journal_bad_idea_ref")  # D56
    return e


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._ids: dict[str, dict] = {}
        self._good = 0
        self.warnings: list[dict] = []
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
        e["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        return self._write([e], fresh=True)[0]

    def merge(self, events: list[dict]) -> int:
        """Restore (U6): events of a backup journal keep their ts; known event_id are skipped."""
        valid = [_validate(e, with_ts=True) for e in events]
        return len(self._write(valid, fresh=False))

    def _write(self, events: list[dict], fresh: bool) -> list[dict]:
        """fresh: return the stored event even when its event_id is known; else only new ones."""
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._catch_up(fd)
                if fresh and events[0]["event_id"] in self._ids:
                    return [dict(self._ids[events[0]["event_id"]])]
                new = list({e["event_id"]: e for e in events
                            if e["event_id"] not in self._ids}.values())
                if not new:
                    return []
                self._set_aside_torn_tail(fd)
                data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in new).encode()
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            self._good += len(data)
            self._events += new
            for e in new:
                self._ids[e["event_id"]] = e
            return [dict(e) for e in new]

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


def session_spans(events: list[dict], session_id: str) -> list[dict]:
    """One session's span events in time order; build once per view, not once per clip."""
    spans = [e for e in events if e.get("session_id") == session_id and e.get("kind") in SPAN_KINDS]
    return sorted(spans, key=_when)  # merged backup events are older than local ones


def clip_view(events: list[dict], session_id: str, start_s: float, end_s: float) -> dict:
    return span_view(session_spans(events, session_id), start_s, end_s)


def span_view(spans: list[dict], start_s: float, end_s: float) -> dict:
    last: dict[tuple, dict] = {}
    heard = []
    for e in spans:
        if not (e["start_s"] < end_s and e["end_s"] > start_s):
            continue
        if e["kind"] == "heard":
            heard.append((e["start_s"], e["end_s"]))
        else:
            last[(e["start_s"], e["end_s"], e["kind"])] = e
    marks = [e["value"] for k, e in last.items() if k[2] == "mark" and e["value"]]
    own = (start_s, end_s)  # D47: the server writes a clip's own span from its clip table
    notes = [
        {"event_id": e["event_id"], "value": e["value"], "own": k[:2] == own}
        for k, e in last.items()
        if k[2] == "note" and e["value"]
    ]
    own_mark = last.get((*own, "mark"), {}).get("value", "")
    covered = total(intersect(union(heard), [(start_s, end_s)]))
    return {
        "marks": list(dict.fromkeys(marks)),
        "notes": notes,
        "heard": covered >= 0.5 * (end_s - start_s),
        "own_mark": own_mark,
    }


def ideas(events: list[dict]) -> list[dict]:
    """Ideas without the ones an idea_deleted event hides (D56; the text stays in the journal)."""
    gone = {e["value"] for e in events if e.get("kind") == "idea_deleted"}
    return [e for e in events if e.get("kind") == "idea" and e["event_id"] not in gone]


def last_backup(events: list[dict]) -> str | None:
    backups = [e for e in events if e.get("kind") == "backup"]
    return max(backups, key=_when)["ts"] if backups else None
