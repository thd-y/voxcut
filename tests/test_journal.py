import json
import os
import threading
import uuid

import pytest

from voxcut import journal
from voxcut.journal import Journal, JournalError, clip_view, load


def ev(kind="mark", value="моё", sid="s1", start=10.0, end=20.0, **kw):
    e = {"event_id": uuid.uuid4().hex, "kind": kind, "value": value}
    if sid is not None:
        e |= {"session_id": sid, "start_s": start, "end_s": end}
    return e | kw


def test_append_fsync_before_return(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(journal.os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev())
    assert calls, "append returned without fsync"


def test_append_idempotent_event_id(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    e = ev()
    first = j.append(dict(e))
    again = j.append(dict(e, value="вокал"))
    assert again == first
    assert len(p.read_text().splitlines()) == 1
    # another instance (second window) sees the same file
    assert Journal(p).append(dict(e)) == first
    assert len(p.read_text().splitlines()) == 1


def test_concurrent_append_threads(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)

    def work():
        for _ in range(50):
            j.append(ev(kind="note", value="x" * 3000))

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = p.read_text().splitlines()
    assert len(lines) == 400
    ids = {json.loads(line)["event_id"] for line in lines}
    assert len(ids) == 400


def test_broken_last_line_skipped(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    j.append(ev())
    j.append(ev(value="шум"))
    with open(p, "a") as f:
        f.write('{"event_id": "ab')
    events, warnings = load(p)
    assert [e["value"] for e in events] == ["моё", "шум"]
    assert len(warnings) == 1
    j2 = Journal(p)
    assert len(j2.events()) == 2 and len(j2.warnings) == 1


def test_append_after_torn_tail_keeps_journal_loadable(tmp_path):
    p = tmp_path / "j.jsonl"
    Journal(p).append(ev())
    with open(p, "a") as f:
        f.write('{"event_id": "ab')
    j = Journal(p)
    j.append(ev(value="шум"))
    events, warnings = load(p)
    assert [e["value"] for e in events] == ["моё", "шум"] and warnings == []
    assert (tmp_path / "j.jsonl.torn").read_text() == '{"event_id": "ab\n'


def test_broken_middle_line_refused(tmp_path):
    p = tmp_path / "j.jsonl"
    good = json.dumps(ev()) + "\n"
    p.write_text(good + "garbage\n" + good)
    with pytest.raises(JournalError):
        load(p)
    with pytest.raises(JournalError):
        Journal(p)


def test_append_rejects_bad_events(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    for bad in (
        ev(kind="exec"),
        ev(value="x" * (journal.MARK_MAX + 1)),
        ev(kind="note", value="x" * (journal.NOTE_MAX + 1)),
        ev(event_id="../x"),
        ev(start=20.0, end=10.0),
    ):
        with pytest.raises(JournalError):
            j.append(bad)


def test_mark_last_wins_and_clear(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(value="моё"))
    j.append(ev(value="вокал"))
    assert clip_view(j.events(), "s1", 10.0, 20.0)["marks"] == ["вокал"]
    j.append(ev(value=""))
    assert clip_view(j.events(), "s1", 10.0, 20.0)["marks"] == []


def test_marks_survive_recut(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(value="моё", start=10.0, end=20.0))
    j.append(ev(kind="note", value="заметка", start=10.0, end=20.0))
    events = j.events()
    assert clip_view(events, "s1", 5.0, 12.0)["marks"] == ["моё"]
    assert clip_view(events, "s1", 18.0, 30.0)["marks"] == ["моё"]
    assert clip_view(events, "s1", 18.0, 30.0)["notes"][0]["value"] == "заметка"
    assert clip_view(events, "s1", 20.0, 25.0) == {"marks": [], "notes": [], "heard": False,
                                                   "own_mark": ""}
    assert clip_view(events, "s2", 5.0, 12.0)["marks"] == []


def test_own_mark_and_note(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(value="моё"))
    j.append(ev(kind="note", value="своя"))
    j.append(ev(value="вокал", start=12.0, end=18.0))
    j.append(ev(kind="note", value="чужая", start=12.0, end=18.0))
    v = clip_view(j.events(), "s1", 10.0, 20.0)
    assert sorted(v["marks"]) == ["вокал", "моё"] and v["own_mark"] == "моё"
    assert {n["value"]: n["own"] for n in v["notes"]} == {"своя": True, "чужая": False}
    assert clip_view(j.events(), "s1", 12.0, 18.0)["own_mark"] == "вокал"
    j.append(ev(value=""))
    assert clip_view(j.events(), "s1", 10.0, 20.0)["own_mark"] == ""


def test_heard_half_coverage(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(kind="heard", value="", start=0.0, end=5.0))
    assert clip_view(j.events(), "s1", 0.0, 10.0)["heard"] is True
    j2 = Journal(tmp_path / "j2.jsonl")
    j2.append(ev(kind="heard", value="", start=0.0, end=4.9))
    assert clip_view(j2.events(), "s1", 0.0, 10.0)["heard"] is False


def test_ideas_and_last_backup(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(kind="idea", value="первая", sid=None))
    j.append(ev(kind="backup", value="full", sid=None))
    j.append(ev(kind="idea", value="вторая", sid=None, session_id="s1"))
    events = j.events()
    assert [e["value"] for e in journal.ideas(events)] == ["первая", "вторая"]
    assert journal.last_backup(events) == events[1]["ts"]
    assert journal.last_backup([]) is None


def _proc_append(path, n):
    j = Journal(path)
    for _ in range(n):
        j.append(ev(kind="note", value="y" * 2000))


def test_two_processes_append(tmp_path):
    """Review focus 5: two windows = two server processes writing one journal."""
    import multiprocessing as mp

    p = tmp_path / "j.jsonl"
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_proc_append, args=(p, 50)) for _ in range(4)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    assert all(pr.exitcode == 0 for pr in procs)
    events, warnings = load(p)
    assert len(events) == 200 and warnings == []
    assert len({e["event_id"] for e in events}) == 200


def test_merge_keeps_ts_and_newer_local_wins(tmp_path):
    """Restore (U6): backup events keep their own ts; a newer local correction is not overridden."""
    old = Journal(tmp_path / "old.jsonl")
    old.append(ev(value="моё"))
    old.append(ev(kind="backup", value="voxcut-backup_x_full.tar", sid=None))
    backup_events = old.events()
    j = Journal(tmp_path / "j.jsonl")
    j.append(ev(value="шум"))
    local_backup = j.append(ev(kind="backup", value="voxcut-backup_y_light.tar", sid=None))
    assert j.merge(backup_events) == 2
    assert j.merge(backup_events) == 0  # replay adds nothing
    events = j.events()
    assert clip_view(events, "s1", 10.0, 20.0)["marks"] == ["шум"]
    assert journal.last_backup(events) == local_backup["ts"]
    assert journal.last_backup(backup_events) == backup_events[1]["ts"]
    assert len(load(tmp_path / "j.jsonl")[0]) == 4


def test_merge_rejects_events_without_valid_ts(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    for bad in (ev(), ev(ts="вчера"), ev(ts="2026-10-21T10:00:00")):  # naive ts is ambiguous
        with pytest.raises(JournalError):
            j.merge([bad])
    assert j.events() == []


# --- D56: idea_deleted ---

def test_idea_deleted_hides_idea(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    first = j.append(ev(kind="idea", value="первая", sid=None))
    j.append(ev(kind="idea", value="вторая", sid=None))
    j.append(ev(kind="idea_deleted", value=first["event_id"], sid=None))
    events = j.events()
    assert [e["value"] for e in journal.ideas(events)] == ["вторая"]
    assert [e["value"] for e in events if e["kind"] == "idea"] == ["первая", "вторая"]  # text kept


@pytest.mark.parametrize("extra", [{"value": "nothex"}, {"session_id": "s1"},
                                   {"start_s": 1.0, "end_s": 2.0}])
def test_idea_deleted_validation(tmp_path, extra):
    j = Journal(tmp_path / "j.jsonl")
    e = ev(kind="idea_deleted", value="a" * 32, sid=None) | extra
    with pytest.raises(JournalError):
        j.append(e)


def test_idea_deleted_survives_reload(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    idea = j.append(ev(kind="idea", value="x", sid=None))
    j.append(ev(kind="idea_deleted", value=idea["event_id"], sid=None))
    again = Journal(p)
    assert journal.ideas(again.events()) == [] and again.warnings == []
