import dataclasses
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from test_session import T0, chain_starts, make_folder

from voxcut import audio
from voxcut import library as library_mod
from voxcut.audio import RunError, SourceError, sha256_file
from voxcut.intervals import SessionError
from voxcut.journal import Journal
from voxcut.library import (
    Layout,
    _drop_empty_source,
    clip_table,
    delete_session,
    finish_import,
    import_originals,
    inbox_inventory,
    list_sessions,
    process_session,
    resolve_clip,
    session_row,
    session_runs,
    sweep_deleted,
)

SID = "2026-08-11_21-30_4h16m.0badface"
SID2 = "2026-08-12_08-00_1h00m.0badcafe"


def make_seg(lay, sid, cfg="2840807d", n=3, name="v1", raw="0badc0de.c0.raw"):
    seg = lay.sessions / sid / f"{sid.split('.')[0]}.{raw}" / f"seg-{cfg}"
    (seg / "clips").mkdir(parents=True)
    (seg / "run.json").write_text(json.dumps({"meta": {"name": name}}))
    rows = []
    for i in range(n):
        (seg / "clips" / f"{i:04d}.wav").write_bytes(b"RIFF")
        rows.append({"start_s": i * 10.0, "end_s": i * 10.0 + 5, "audio_path": f"clips/{i:04d}.wav"})
    (seg / "manifest.jsonl").write_text("")
    (seg / "clips" / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return seg


@pytest.fixture
def lay(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    return lay


def test_layout_ensure(lay):
    for d in (lay.inbox, lay.sessions, lay.journal.parent, lay.backups, lay.jobs):
        assert d.is_dir()
    assert (lay.run.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("case", ["dotdot", "absolute", "absolute_inside", "symlink_out", "directory", "missing"])
def test_resolve_clip_refuses(lay, case):
    seg = make_seg(lay, SID)
    outside = lay.root / "secret.wav"
    outside.write_bytes(b"x")
    path = {
        "dotdot": "../../../../../secret.wav",
        "absolute": str(outside),
        "absolute_inside": str(seg / "clips" / "0001.wav"),
        "symlink_out": "clips/link.wav",
        "directory": "clips",
        "missing": "clips/nope.wav",
    }[case]
    if case == "symlink_out":
        os.symlink(outside, seg / "clips" / "link.wav")
    assert resolve_clip(seg, path) is None


def test_resolve_clip_ok(lay):
    seg = make_seg(lay, SID)
    assert resolve_clip(seg, "clips/0001.wav") == (seg / "clips" / "0001.wav").resolve()


def test_clip_ids_stable_and_opaque(lay):
    seg = make_seg(lay, SID)
    seg2 = make_seg(lay, SID2)  # same seg-<cfg8> name in another session
    a, b = clip_table(lay, seg), clip_table(lay, seg)
    assert [r["clip_id"] for r in a] == [r["clip_id"] for r in b]
    assert all("/" not in r["clip_id"] and len(r["clip_id"]) == 16 for r in a)
    assert [r["n"] for r in a] == [0, 1, 2]
    ids2 = {r["clip_id"] for r in clip_table(lay, seg2)}
    assert not ids2 & {r["clip_id"] for r in a}


def test_clip_table_bad_path_kept_without_file(lay):
    seg = make_seg(lay, SID)
    m = seg / "clips" / "manifest.jsonl"
    rows = [json.loads(x) for x in m.read_text().splitlines()]
    rows[1]["audio_path"] = "../run.json"
    m.write_text("".join(json.dumps(r) + "\n" for r in rows))
    t = clip_table(lay, seg)
    assert len(t) == 3 and t[1]["path"] is None and t[0]["path"] is not None


def test_list_sessions_states(lay):
    def sdir(sid):
        d = lay.sessions / sid
        d.mkdir()
        return d

    s = "2026-10-01_10-00_1h00m.0000000"
    imported = sdir(s + "1")
    (imported / "import.json").write_text("{}")
    (imported / "originals").mkdir()
    light = sdir(s + "2")
    (light / "import.json").write_text("{}")
    torn = sdir(s + "3")
    (torn / "originals").mkdir()
    tmp_left = sdir(s + "4")
    (tmp_left / "import.json").write_text("{}")
    (tmp_left / "originals.tmp").mkdir()
    sdir("not-a-session")
    clips = sdir(SID)
    (clips / "import.json").write_text("{}")
    sess = clips / f"{SID}.session"
    sess.mkdir()
    (sess / "session.json").write_text(json.dumps({"wav": "x.wav", "n_samples": 32000, "sr": 16000}))
    (sess / "x.wav").write_bytes(b"RIFF")
    make_seg(lay, SID, n=4)

    got = {r["session_id"]: r for r in list_sessions(lay)}
    assert set(got) == {s + "1", s + "2", s + "3", s + "4", SID}
    assert got[s + "1"]["state"] == "imported" and got[s + "1"]["can_process"]
    assert got[s + "2"]["state"] == "imported" and not got[s + "2"]["can_process"]
    assert got[s + "3"]["state"] == "incomplete" and not got[s + "3"]["can_process"]
    assert got[s + "4"]["state"] == "incomplete" and not got[s + "4"]["can_process"]
    c = got[SID]
    assert c["state"] == "clips" and c["can_process"]
    assert c["name"] == "2026-08-11_21-30_4h16m"
    assert c["duration_s"] == 2.0 and c["n_clips"] == 4
    assert got[s + "1"]["duration_s"] is None and got[s + "1"]["n_clips"] is None


def test_list_sessions_intermediate_states(lay):
    d = lay.sessions / SID
    sess = d / f"{SID}.session"
    sess.mkdir(parents=True)
    (d / "import.json").write_text("{}")
    (sess / "session.json").write_text(json.dumps({"wav": "x.wav", "n_samples": 16000, "sr": 16000}))
    assert list_sessions(lay)[0]["state"] == "assembled"
    raw = d / "2026-08-11_21-30_4h16m.0badc0de.c0.raw"
    raw.mkdir()
    (raw / "probs.json").write_text("{}")
    assert list_sessions(lay)[0]["state"] == "probs"
    (raw / "seg-aaaaaaaa").mkdir()
    (raw / "seg-aaaaaaaa" / "manifest.jsonl").write_text("")
    assert list_sessions(lay)[0]["state"] == "segmented"


def test_session_runs_newest_first(lay):
    old = make_seg(lay, SID, cfg="11111111", name="old")
    new = make_seg(lay, SID, cfg="22222222", name="new")
    os.utime(old / "clips" / "manifest.jsonl", (1_000_000, 1_000_000))
    os.utime(new / "clips" / "manifest.jsonl", (2_000_000, 2_000_000))
    runs = session_runs(lay, SID)
    assert [r["name"] for r in runs] == ["new", "old"]
    assert runs[0]["seg_dir"] == new
    assert len(runs[0]["run_id"]) == 12 and runs[0]["run_id"] != runs[1]["run_id"]
    assert session_runs(lay, SID2) == []


def test_list_sessions_broken_session_does_not_hide_others(lay):
    good = lay.sessions / SID
    good.mkdir()
    (good / "import.json").write_text("{}")
    (good / "originals").mkdir()
    bad = make_seg(lay, SID2)
    (lay.sessions / SID2 / "import.json").write_text("{}")
    (bad / "run.json").unlink()
    got = {r["session_id"]: r for r in list_sessions(lay)}
    assert got[SID]["state"] == "imported" and got[SID]["error"] is None
    assert got[SID2]["state"] == "incomplete" and not got[SID2]["can_process"]
    assert got[SID2]["error"]["code"] == "session_unreadable"
    assert "run.json" in got[SID2]["error"]["params"]["detail"]


def test_session_row_matches_list(lay):
    good = lay.sessions / SID
    good.mkdir()
    (good / "import.json").write_text("{}")
    bad = make_seg(lay, SID2)
    (lay.sessions / SID2 / "import.json").write_text("{}")
    (bad / "run.json").unlink()
    for r in list_sessions(lay):
        assert session_row(lay, r["session_id"]) == r
    assert "run.json" in session_row(lay, SID2)["error"]["params"]["detail"]
    (lay.data / "pilot").mkdir()  # a real dir outside sessions/
    for sid in ("../pilot", "..", "2026-08-11_21-30_0h00m.00000000"):
        assert session_row(lay, sid) is None


# --- import from inbox/ (UI task 5, U4, D39) ---

@pytest.fixture
def no_check(monkeypatch):
    """check_source decodes with ffmpeg (D18); the slow test runs it for real."""
    monkeypatch.setattr(audio, "check_source", lambda p: None)


def _shas(paths):
    return {p.name: sha256_file(str(p)) for p in paths}


def _quiet(_msg, level="INFO"):
    pass


def _rec(out):
    """A log callback that keeps (level, msg)."""
    return lambda m, level="INFO": out.append((level, m))


def test_import_moves_unchanged(lay, no_check):
    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    res = import_originals(lay, "", 1, None, _quiet)
    assert res["existing"] is False
    sdir = lay.sessions / res["session_id"]
    assert _shas(sorted((sdir / "originals").iterdir())) == before
    assert list(lay.inbox.iterdir()) == []
    meta = json.loads((sdir / "import.json").read_text())
    assert {f["name"]: f["sha256"] for f in meta["files"]} == before
    assert meta["chain"] == 1 and meta["start"] is None and meta["source"] == ""
    assert all(f["size_bytes"] > 0 for f in meta["files"]) and meta["imported_at"]
    assert [r["state"] for r in list_sessions(lay)] == ["imported"]


def test_import_from_subfolder(lay, no_check):
    make_folder(lay.inbox / "folder01", chain_starts(2))
    res = import_originals(lay, "folder01", 1, "2026-08-11 21:30", _quiet)
    assert res["session_id"].startswith("2026-08-11_21-30_")
    assert not (lay.inbox / "folder01").exists()  # D50: the emptied source folder is removed
    assert json.loads((lay.sessions / res["session_id"] / "import.json").read_text())[
        "start"] == "2026-08-11 21:30"


def test_import_removes_empty_source_dir(lay, no_check):
    make_folder(lay.inbox / "13_oct", chain_starts(2))
    import_originals(lay, "13_oct", 1, None, _quiet)
    assert not (lay.inbox / "13_oct").exists() and lay.inbox.is_dir()


def test_import_keeps_leftovers_and_warns(lay, no_check):
    make_folder(lay.inbox / "13_oct", chain_starts(2))
    extra = {"desktop.ini": b"[x]", "x.wav:Zone.Identifier": b"zone"}
    for name, data in extra.items():
        (lay.inbox / "13_oct" / name).write_bytes(data)
    msgs = []
    import_originals(lay, "13_oct", 1, None, _rec(msgs))
    assert {p.name: p.read_bytes() for p in (lay.inbox / "13_oct").iterdir()} == extra
    warn = [(lv, m) for lv, m in msgs if m.startswith("inbox/")]
    assert warn == [("WARNING", ("inbox/13_oct/ still has files: desktop.ini, "
                                 "x.wav:Zone.Identifier; the Library no longer shows the folder, delete it by hand"))]
    assert ("INFO", f"imported 2 files: session {list_sessions(lay)[0]['session_id']}") in msgs


def test_import_rmdir_race_is_not_failure(lay, no_check, monkeypatch):
    make_folder(lay.inbox / "13_oct", chain_starts(2))

    def busy(path, *a, **kw):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(os, "rmdir", busy)
    msgs = []
    res = import_originals(lay, "13_oct", 1, None, _rec(msgs))
    assert res["existing"] is False
    assert any(m.startswith("inbox/13_oct/ still has files") for _, m in msgs)


def test_import_second_chain_left_without_warning(lay, no_check):
    make_folder(lay.inbox / "13_oct", chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    msgs = []
    import_originals(lay, "13_oct", 1, None, _rec(msgs))
    left = sorted(p.name for p in (lay.inbox / "13_oct").iterdir())
    assert len(left) == 1 and left[0].lower().endswith(".wav")  # chain 2 is still there
    assert not any(m.startswith("inbox/") for _, m in msgs)  # never «delete it by hand» on a WAV
    assert [s["source"] for s in inbox_inventory(lay)["sources"]] == ["13_oct"]


def test_import_pinned_to_files_not_position(lay, no_check):
    """Review #1: chain numbers shift once an earlier chain leaves inbox/."""
    make_folder(lay.inbox, chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    c1, c2 = [c["files"] for c in inbox_inventory(lay)["sources"][0]["chains"]]
    import_originals(lay, "", 1, None, _quiet, files=c1)
    res = import_originals(lay, "", 2, None, _quiet, files=c2)  # queued while chain 1 ran
    sdir = lay.sessions / res["session_id"]
    assert sorted(p.name for p in (sdir / "originals").iterdir()) == c2
    assert json.loads((sdir / "import.json").read_text())["chain"] == 1  # the chain it now is


def test_import_symlink_refused_before_move(lay, no_check, tmp_path):
    """Review #3: os.rename would move only the link; the recording would stay outside data/."""
    paths = make_folder(lay.inbox, chain_starts(2))
    real = tmp_path / "elsewhere.WAV"
    os.rename(paths[1], real)
    paths[1].symlink_to(real)
    with pytest.raises(SessionError, match="a link, not a file") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_symlink"
    assert paths[0].is_file() and paths[1].is_symlink() and list(lay.sessions.iterdir()) == []


def test_import_inbox_changed_between_reads_refused(lay, no_check, monkeypatch):
    make_folder(lay.inbox, chain_starts(2))
    monkeypatch.setattr(audio, "list_chains", lambda src: [["20250301082921.WAV"]])  # stale read
    with pytest.raises(SessionError, match="the inbox changed") as e:
        import_originals(lay, "", 1, None, _quiet, files=["20250301082921.WAV"])
    assert e.value.code == "import_inbox_changed"
    assert list(lay.sessions.iterdir()) == []


def test_import_repeat_of_gone_chain_refused(lay, no_check):
    make_folder(lay.inbox, chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    c1 = inbox_inventory(lay)["sources"][0]["chains"][0]["files"]
    import_originals(lay, "", 1, None, _quiet, files=c1)
    left = sorted(p.name for p in lay.inbox.iterdir())
    with pytest.raises(SessionError, match="is not in the inbox") as e:
        import_originals(lay, "", 1, "2026-08-11 21:30", _quiet, files=c1)  # double click
    assert e.value.code == "import_chain_gone"
    assert sorted(p.name for p in lay.inbox.iterdir()) == left


def test_inventory_unreadable_subfolder_skipped(lay, monkeypatch):
    make_folder(lay.inbox / "folder01", chain_starts(2))
    (lay.inbox / "locked").mkdir()
    real = type(lay.inbox).iterdir

    def iterdir(self):
        if self.name == "locked":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(type(lay.inbox), "iterdir", iterdir)
    assert [s["source"] for s in inbox_inventory(lay)["sources"]] == ["folder01"]


def test_import_root_inbox_never_removed(lay, no_check):
    make_folder(lay.inbox, chain_starts(2))
    import_originals(lay, "", 1, None, _quiet)
    assert lay.inbox.is_dir()


@pytest.mark.parametrize("leftover", [[], ["desktop.ini"]])
def test_inventory_hides_dir_without_wav(lay, leftover):
    (lay.inbox / "13_oct").mkdir(parents=True)
    for name in leftover:
        (lay.inbox / "13_oct" / name).write_bytes(b"x")
    make_folder(lay.inbox / "folder01", chain_starts(2))
    assert [s["source"] for s in inbox_inventory(lay)["sources"]] == ["folder01"]


@pytest.mark.parametrize("source", ["../data", "folder01/x", "nope", "/tmp"])
def test_import_unknown_source_refused(lay, no_check, source):
    make_folder(lay.inbox / "folder01", chain_starts(2))
    with pytest.raises(SessionError, match="no such source") as e:
        import_originals(lay, source, 1, None, _quiet)
    assert e.value.code == "inbox_no_source"


def test_import_same_sha_existing(lay, no_check):
    paths = make_folder(lay.inbox, chain_starts(2))
    copies = {p.name: p.read_bytes() for p in paths}
    first = import_originals(lay, "", 1, None, _quiet)
    for name, data in copies.items():
        (lay.inbox / name).write_bytes(data)
    for p in paths:
        os.utime(p, (p.stat().st_mtime, p.stat().st_mtime))
    again = import_originals(lay, "", 1, None, _quiet)
    assert again == {"session_id": first["session_id"], "existing": True}
    assert sorted(p.name for p in lay.inbox.iterdir()) == sorted(copies)


def test_import_skips_broken_session_json(lay, no_check):
    broken = lay.sessions / SID2
    broken.mkdir(parents=True)
    (broken / "import.json").write_text("{")
    (lay.sessions / "junk").mkdir()
    (lay.sessions / "junk" / "import.json").write_text("{}")
    make_folder(lay.inbox, chain_starts(2))
    logged = []
    got = import_originals(lay, "", 1, None, _rec(logged))
    assert (lay.sessions / got["session_id"] / "import.json").is_file()
    assert [(lv, m) for lv, m in logged if SID2 in m] == [
        ("WARNING", (f"{SID2}: import.json is unreadable (JSONDecodeError), "
                     "the re-import check skips it"))]
    assert not any("junk" in m for _, m in logged)


def test_import_partial_overlap_refused(lay, no_check):
    paths = make_folder(lay.inbox, chain_starts(2))
    keep = paths[0].read_bytes()
    import_originals(lay, "", 1, None, _quiet)
    make_folder(lay.inbox, chain_starts(2), seed=7)
    (lay.inbox / paths[0].name).write_bytes(keep)
    with pytest.raises(SessionError, match=paths[0].name) as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_partly_imported"
    assert len(list(lay.inbox.iterdir())) == 2


def test_import_truncated_refused_stays(lay, no_check):
    """Review focus 2: the file is still being copied by Explorer when «импорт» is pressed."""
    paths = make_folder(lay.inbox, chain_starts(2))
    raw = paths[1].read_bytes()
    paths[1].write_bytes(raw[: len(raw) // 2])
    with pytest.raises((SourceError, SessionError)):
        import_originals(lay, "", 1, None, _quiet)
    assert sorted(p.name for p in lay.inbox.iterdir()) == sorted(p.name for p in paths)
    assert list(lay.sessions.iterdir()) == []


def test_import_sha_mismatch_after_move(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(2))
    real, seen = audio.sha256_file, set()

    def flaky(p):
        name = os.path.basename(p)
        if name in seen:
            return "0" * 64
        seen.add(name)
        return real(p)

    monkeypatch.setattr(audio, "sha256_file", flaky)
    with pytest.raises(SessionError, match="sha256") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "finish_sha_mismatch"
    sid = next(lay.sessions.iterdir()).name
    assert sorted(p.name for p in (lay.sessions / sid / "originals").iterdir()) == sorted(
        p.name for p in paths)
    assert list_sessions(lay)[0]["state"] == "incomplete"


def test_import_unfinished_tmp_refused(lay, no_check):
    make_folder(lay.inbox, chain_starts(2))
    sid = inbox_inventory(lay)["sources"][0]["chains"][0]["session_id"]
    (lay.sessions / sid / "originals.tmp").mkdir(parents=True)
    with pytest.raises(SessionError, match="originals.tmp") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_dir_unfinished"
    assert len(list(lay.inbox.iterdir())) == 2


def test_inventory_two_chains_and_backup(lay):
    make_folder(lay.inbox / "folder01", chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    make_folder(lay.inbox / "broken", chain_starts(2, gap=60))
    (lay.inbox / "voxcut-backup_2026-10-21_10-00_full.tar").write_bytes(b"")
    (lay.inbox / "other.tar").write_bytes(b"")
    inv = inbox_inventory(lay)
    assert inv["backups"] == ["voxcut-backup_2026-10-21_10-00_full.tar"]
    src = {s["source"]: s for s in inv["sources"]}
    assert set(src) == {"folder01", "broken"}  # inbox/ root has no WAV
    assert src["broken"]["error"]["code"] == "session_pause" and src["broken"]["chains"] == []
    s1 = src["folder01"]
    assert s1["error"] is None
    assert [c["chain"] for c in s1["chains"]] == [1, 2]
    assert s1["chains"][0]["files"] == ["20250301082921.WAV", "20250301082924.WAV"]
    assert s1["chains"][1]["start_local"].startswith("2025-03-01T10:29")
    assert s1["chains"][0]["duration_s"] == 4.0 and not s1["chains"][0]["imported"]


def test_inventory_reads_headers_once_per_source(lay, monkeypatch):
    """Review #6: /api/state polls the inbox every 2 s; one header pass per folder."""
    make_folder(lay.inbox / "folder01", chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    calls = []
    real = audio._inventory
    monkeypatch.setattr(audio, "_inventory", lambda src: calls.append(src) or real(src))
    chains = inbox_inventory(lay)["sources"][0]["chains"]
    assert [c["files"] for c in chains] == [
        ["20250301082921.WAV", "20250301082924.WAV"], ["20250301102921.WAV"]]
    assert len(calls) == 1


@pytest.mark.slow
def test_import_real_check_source(lay):
    paths = make_folder(lay.inbox, chain_starts(2))
    before = _shas(paths)
    res = import_originals(lay, "", 1, None, _quiet)
    assert _shas(sorted((lay.sessions / res["session_id"] / "originals").iterdir())) == before


# --- processing (UI task 6, U8) ---

def _import(lay, start="2026-08-11 21:30", **kw):
    make_folder(lay.inbox, chain_starts(3), **kw)
    return import_originals(lay, "", 1, start, _quiet)["session_id"]


def test_process_light_refused(lay):
    sid = "2026-08-11_21-30_0h00m.12345678"
    (lay.sessions / sid).mkdir()
    (lay.sessions / sid / "import.json").write_text(json.dumps({"start": None}))
    with pytest.raises(RunError, match="light") as e:
        process_session(lay, sid, lambda s: None, _quiet)
    assert e.value.code == "process_light"


def test_process_exports_from_own_session(lay, monkeypatch):
    """D64: export reads this session's WAV, not the absolute path stored in run.json."""
    sdir = lay.sessions / SID
    sess = sdir / f"{SID}.session"
    sess.mkdir(parents=True)
    (sdir / "import.json").write_text(json.dumps({"start": None, "files": []}))
    (sess / "session.json").write_text(json.dumps({"wav": "x.wav", "n_samples": 1, "sr": 1}))
    (sess / "x.wav").write_bytes(b"RIFF")
    seg = sdir / "x.c0.raw" / "seg-aaaaaaaa"
    seg.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(audio, "compute_probs", lambda src, out: sdir / "x.c0.raw")
    monkeypatch.setattr("voxcut.library.segment", lambda pdir, cfg, meta: seg)
    monkeypatch.setattr(audio, "export_clips", lambda *a, **k: calls.append((a, k)))
    process_session(lay, SID, lambda s: None, _quiet)
    assert calls == [((str(seg), str(sess / "x.wav")), {})]


@pytest.mark.parametrize("sid", ["../x", "2026-08-11_21-30_0h00m.00000000"])
def test_process_unknown_session_refused(lay, sid):
    with pytest.raises(RunError, match="no session") as e:
        process_session(lay, sid, lambda s: None, _quiet)
    assert e.value.code == "process_no_session"


@pytest.mark.slow
def test_process_end_to_end(lay):
    from test_session import same_channels

    sid = _import(lay, data=same_channels)
    stages = []
    clips = process_session(lay, sid, stages.append, _quiet)
    assert stages == ["assemble", "probs", "segment", "export"]
    assert (clips / "manifest.jsonl").is_file()
    row = list_sessions(lay)[0]
    assert row["state"] == "clips" and row["n_clips"] == len(
        (clips / "manifest.jsonl").read_text().splitlines())
    assert json.loads((clips.parent / "run.json").read_text())["meta"]["name"] == "v1"
    # D6, D19: run dirs hold only what segment/export wrote; marks go to the journal
    assert {p.name for p in clips.parent.iterdir()} <= {
        "run.json", "raw.jsonl", "manifest.jsonl", "seams.jsonl", "clips"}


@pytest.mark.slow
def test_process_idempotent(lay):
    from test_session import same_channels

    sid = _import(lay, data=same_channels)
    clips = process_session(lay, sid, lambda s: None, _quiet)
    before = {p: p.stat().st_mtime_ns for p in (lay.sessions / sid).rglob("*")}
    stages = []
    assert process_session(lay, sid, stages.append, _quiet) == clips
    assert "export" not in stages and "assemble" not in stages
    assert {p: p.stat().st_mtime_ns for p in (lay.sessions / sid).rglob("*")} == before


def test_import_never_copies_across_filesystems(lay, no_check, monkeypatch):
    """D39: only a rename; across filesystems it refuses instead of copy + delete."""
    import errno

    paths = make_folder(lay.inbox, chain_starts(2))

    def exdev(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", exdev)
    with pytest.raises(SessionError, match="files are in the inbox") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_move_failed"
    assert sorted(p.name for p in lay.inbox.iterdir()) == sorted(p.name for p in paths)
    assert not any(lay.sessions.iterdir())


def _rename_failing(monkeypatch, fail_forward_at, fail_back=False):
    """EXDEV on the n-th inbox -> originals.tmp rename (1-based); optionally on every way back."""
    import errno

    real, n = os.rename, [0]

    def rename(src, dst):
        back = "originals.tmp" in str(src)
        if not back:
            n[0] += 1
        if (not back and n[0] == fail_forward_at) or (back and fail_back):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)


def test_import_rollback_after_partial_move(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    _rename_failing(monkeypatch, 2)
    with pytest.raises(SessionError, match="files are in the inbox") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_move_failed"
    assert _shas(paths) == before
    assert not any(lay.sessions.iterdir())


def test_import_rollback_failure_keeps_pending(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    _rename_failing(monkeypatch, 2, fail_back=True)
    with pytest.raises(SessionError, match="originals.tmp") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_move_stuck"
    monkeypatch.undo()
    sdir = next(lay.sessions.iterdir())
    moved = sorted(p.name for p in (sdir / "originals.tmp").iterdir())
    assert moved == [paths[0].name]
    assert (sdir / "import.pending.json").is_file()
    after = {p.name: sha256_file(str(p)) for p in [*paths[1:], sdir / "originals.tmp" / moved[0]]}
    assert after == before


def _sigterm_mid_move(lay, monkeypatch, at=2, source=""):
    """SIGTERM on the at-th inbox -> originals.tmp rename of a fresh import."""
    real, n = os.rename, [0]

    def rename(src, dst):
        n[0] += 1
        if n[0] == at:
            raise SystemExit(143)
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(SystemExit):
        import_originals(lay, source, 1, None, _quiet)
    monkeypatch.undo()


def test_import_sigterm_mid_move_left_for_process(lay, no_check, monkeypatch):
    """SystemExit is not rolled back: the pending import stays for «Обработать» (finish_import)."""
    paths = make_folder(lay.inbox, chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch)
    sdir = next(lay.sessions.iterdir())
    assert (sdir / "import.pending.json").is_file()
    assert [p.name for p in (sdir / "originals.tmp").iterdir()] == [paths[0].name]


def test_sigterm_mid_move_finished_by_process(lay, no_check, monkeypatch):
    """D65: «Обработать» brings the rest of the chain from inbox/, sha256-checked."""
    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    _sigterm_mid_move(lay, monkeypatch)
    row = list_sessions(lay)[0]
    assert row["can_process"]
    finish_import(lay, row["session_id"], _quiet)
    sdir = lay.sessions / row["session_id"]
    assert _shas(sorted((sdir / "originals").iterdir())) == before
    assert (sdir / "import.json").is_file()
    assert not any(p.exists() for p in paths)


def test_finish_move_failure_names_file(lay, no_check, monkeypatch):
    import errno

    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    _sigterm_mid_move(lay, monkeypatch)
    sid = list_sessions(lay)[0]["session_id"]
    real, n = os.rename, [0]

    def rename(src, dst):
        if str(lay.inbox) in str(src):
            n[0] += 1
            if n[0] == 2:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(SessionError, match=paths[2].name) as e:
        finish_import(lay, sid, _quiet)
    assert e.value.code == "finish_move_failed"
    assert "inbox" in str(e.value) and "originals" in str(e.value)
    monkeypatch.undo()
    finish_import(lay, sid, _quiet)
    assert _shas(sorted((lay.sessions / sid / "originals").iterdir())) == before


@pytest.mark.parametrize("leftover", [False, True])
def test_finish_removes_empty_source_folder(lay, no_check, monkeypatch, leftover):
    make_folder(lay.inbox / "rec", chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch, source="rec")
    if leftover:
        (lay.inbox / "rec" / "notes.txt").write_text("x")
    logged = []
    finish_import(lay, list_sessions(lay)[0]["session_id"], _rec(logged))
    assert (lay.inbox / "rec").exists() is leftover
    assert any(lv == "WARNING" and "delete it by hand" in m for lv, m in logged) is leftover


def test_finish_failed_check_keeps_source_folder(lay, no_check, monkeypatch):
    """The emptied source folder goes only once the import is complete."""
    make_folder(lay.inbox / "rec", chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch, source="rec")
    sid = list_sessions(lay)[0]["session_id"]
    real = audio.plan_session

    def other_session(src, chain, start):
        plan = real(src, chain, start)
        return dataclasses.replace(plan, session_id="2026-01-01_00-00_1h00m.00000000")

    monkeypatch.setattr(audio, "plan_session", other_session)
    (lay.inbox / "rec" / "notes.txt").write_text("x")  # the old order logged «удалите» here
    logged = []
    with pytest.raises(SessionError, match="after the move the session is") as e:
        finish_import(lay, sid, _rec(logged))
    assert e.value.code == "finish_session_mismatch"
    assert (lay.inbox / "rec").is_dir()
    assert not any("delete it by hand" in m for _, m in logged)


def test_finish_retry_after_interrupt_drops_folder(lay, no_check, monkeypatch):
    """Stopped inside the post-move checks, the retry moves nothing yet still drops the folder."""
    make_folder(lay.inbox / "rec", chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch, source="rec")
    sid = list_sessions(lay)[0]["session_id"]

    def sigterm(*a):
        raise SystemExit(143)

    monkeypatch.setattr(audio, "plan_session", sigterm)
    with pytest.raises(SystemExit):
        finish_import(lay, sid, _quiet)
    monkeypatch.undo()
    logged = []
    finish_import(lay, sid, _rec(logged))
    assert (lay.sessions / sid / "import.json").is_file()
    assert not (lay.inbox / "rec").exists()
    assert not any("delete it by hand" in m for _, m in logged)


@pytest.mark.parametrize("source", ["../x", "/abs", ".hidden", "link"])
def test_drop_source_only_plain_inbox_folder(lay, tmp_path, source):
    """A source from a restored import.pending.json never removes a folder outside inbox/."""
    target = {"../x": lay.root / "x", "/abs": tmp_path / "abs"}.get(source, lay.inbox / source)
    if source == "link":
        (tmp_path / "real").mkdir()
        target.symlink_to(tmp_path / "real")
    else:
        target.mkdir()
    if source == "link":
        (tmp_path / "real" / "notes.txt").write_text("x")
    logged = []
    _drop_empty_source(lay, str(tmp_path / "abs") if source == "/abs" else source, _rec(logged))
    assert not logged  # not even «удалите вручную» about what a link points to
    assert target.exists() or target.is_symlink()
    assert (tmp_path / "real").is_dir() or source != "link"


def test_finish_refuses_changed_inbox_file(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch)
    paths[2].write_bytes(paths[2].read_bytes()[:-1] + b"x")
    sid = list_sessions(lay)[0]["session_id"]
    with pytest.raises(SessionError, match=paths[2].name) as e:
        finish_import(lay, sid, _quiet)
    assert e.value.code == "finish_changed"
    assert paths[1].exists() and paths[2].exists()
    assert not (lay.sessions / sid / "import.json").exists()


def test_pending_session_blocks_reimport_of_rest(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch)
    with pytest.raises(SessionError, match="unfinished import") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_unfinished_elsewhere"
    assert paths[1].exists() and paths[2].exists()
    assert len(list(lay.sessions.iterdir())) == 1


def test_rollback_keeps_dangling_symlink(lay, no_check, monkeypatch):
    import errno

    paths = make_folder(lay.inbox, chain_starts(3))
    real, n = os.rename, [0]

    def rename(src, dst):
        n[0] += 1
        if n[0] == 2:
            os.symlink("/nonexistent", paths[0])  # appeared in the slot after the first move
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(SessionError, match="originals.tmp") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_move_stuck"
    monkeypatch.undo()
    assert os.path.islink(paths[0])
    sdir = next(lay.sessions.iterdir())
    assert [p.name for p in (sdir / "originals.tmp").iterdir()] == [paths[0].name]


@pytest.mark.parametrize("bad", ["../x.wav", "ABS", "sub/x.wav"])
def test_pending_names_outside_refused(lay, no_check, monkeypatch, tmp_path, bad):
    """A pending file may come from a backup: only plain file names are looked up (D65)."""
    import shutil

    paths = make_folder(lay.inbox, chain_starts(3))
    _sigterm_mid_move(lay, monkeypatch)
    sdir = next(lay.sessions.iterdir())
    name = str(tmp_path / "abs.wav") if bad == "ABS" else bad
    outside = {"ABS": Path(name), "sub/x.wav": lay.inbox / "sub" / "x.wav"}.get(
        bad, sdir / "x.wav")
    outside.parent.mkdir(exist_ok=True)
    shutil.copyfile(sdir / "originals.tmp" / paths[0].name, outside)  # the same sha256
    meta = json.loads((sdir / "import.pending.json").read_text())
    meta["files"][0]["name"] = name
    (sdir / "import.pending.json").write_text(json.dumps(meta))
    assert list_sessions(lay)[0]["can_process"] is False  # the assertion that does the work
    with pytest.raises(SessionError):
        finish_import(lay, sdir.name, _quiet)
    assert outside.is_file()


def test_rollback_never_overwrites_inbox(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    import errno

    real, n = os.rename, [0]

    def rename(src, dst):
        n[0] += 1
        if n[0] == 2:
            paths[0].write_bytes(b"new file in the inbox")  # appeared after the first move
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(SessionError, match="originals.tmp") as e:
        import_originals(lay, "", 1, None, _quiet)
    assert e.value.code == "import_move_stuck"
    monkeypatch.undo()
    assert paths[0].read_bytes() == b"new file in the inbox"
    sdir = next(lay.sessions.iterdir())
    assert [p.name for p in (sdir / "originals.tmp").iterdir()] == [paths[0].name]


def test_pending_without_files_not_processable(lay, no_check, monkeypatch):
    make_folder(lay.inbox, chain_starts(2))
    _crash_after_move(monkeypatch)
    with pytest.raises(SystemExit):
        import_originals(lay, "", 1, None, _quiet)
    monkeypatch.undo()
    sid = list_sessions(lay)[0]["session_id"]
    sdir = lay.sessions / sid
    for p in (sdir / "originals").iterdir():  # as after a light restore: no recordings
        p.unlink()
    assert list_sessions(lay)[0]["can_process"] is False
    with pytest.raises(SessionError, match="no recording files in originals") as e:
        finish_import(lay, sid, _quiet)
    assert e.value.code == "finish_none_in_originals"
    assert not (sdir / "import.json").exists() and (sdir / "import.pending.json").is_file()


def _crash_after_move(monkeypatch):
    real, seen = audio.sha256_file, set()

    def crash(p):
        name = os.path.basename(p)
        if name in seen:
            raise SystemExit(143)  # SIGTERM / power cut after the move, before import.json
        seen.add(name)
        return real(p)

    monkeypatch.setattr(audio, "sha256_file", crash)


def test_interrupted_import_finished_by_process(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(2))
    before = _shas(paths)
    _crash_after_move(monkeypatch)
    with pytest.raises(SystemExit):
        import_originals(lay, "", 1, None, _quiet)
    monkeypatch.undo()
    row = list_sessions(lay)[0]
    assert row["state"] == "incomplete" and row["can_process"]
    assert row["error"] == {"code": "session_import_pending", "params": {"session": row["session_id"]}}
    sid = row["session_id"]
    finish_import(lay, sid, _quiet)
    meta = json.loads((lay.sessions / sid / "import.json").read_text())
    assert {f["name"]: f["sha256"] for f in meta["files"]} == before
    assert list_sessions(lay)[0]["state"] == "imported"
    assert not (lay.sessions / sid / "import.pending.json").exists()


def test_interrupted_import_sha_mismatch_not_finished(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(2))
    _crash_after_move(monkeypatch)
    with pytest.raises(SystemExit):
        import_originals(lay, "", 1, None, _quiet)
    monkeypatch.undo()
    sid = list_sessions(lay)[0]["session_id"]
    (lay.sessions / sid / "originals" / paths[0].name).write_bytes(b"changed")
    with pytest.raises(SessionError, match="sha256") as e:
        finish_import(lay, sid, _quiet)
    assert e.value.code == "finish_sha_mismatch"
    assert not (lay.sessions / sid / "import.json").exists()


def test_inventory_unreadable_source_keeps_reason(lay, monkeypatch):
    """Layer-2 review: an OSError in one inbox folder still names its cause, as before D66."""
    (lay.inbox / "rec").mkdir()
    (lay.inbox / "rec" / "a.wav").write_bytes(b"")

    def denied(src):
        raise PermissionError(13, "Permission denied", src)

    monkeypatch.setattr(audio, "plan_chains", denied)
    err = inbox_inventory(lay)["sources"][0]["error"]
    assert err["code"] == "inbox_unreadable" and "Permission denied" in err["params"]["detail"]


# --- delete a hidden session: the import in reverse (D90, D93, D94) ---

START = "2025-03-01 09:00"


def note(lay, sid, s, e, value):
    Journal(lay.journal).append({"event_id": uuid.uuid4().hex, "kind": "note", "value": value,
                                 "session_id": sid, "start_s": s, "end_s": e})


def imported(lay, start=START):
    """An imported session with a run, as the UI makes it; returns (sid, sha256 per file)."""
    shas = _shas(make_folder(lay.inbox, chain_starts(2)))
    sid = import_originals(lay, "", 1, start, _quiet)["session_id"]
    make_seg(lay, sid)
    return sid, shas


def assert_deleted(lay, sid, shas):
    back = lay.inbox / sid
    assert _shas(p for p in back.iterdir() if p.suffix == ".WAV") == shas
    assert not (lay.sessions / sid).exists() and not (lay.sessions / f"{sid}.tmp").exists()
    assert not (lay.inbox / f".{sid}.tmp").exists()
    assert list_sessions(lay) == []


def test_delete_returns_originals_and_notes(lay, no_check):
    sid, shas = imported(lay)
    note(lay, sid, 5.0, 7.5, "первая")
    note(lay, sid, 5.0, 7.5, "вторая")  # the latest per span wins
    note(lay, sid, 3600.0, 3725.0, "a\nb")
    note(lay, sid, 60.0, 61.0, "")  # an emptied note is not a note
    note(lay, "other", 1.0, 2.0, "чужая")
    logged = []
    res = delete_session(lay, sid, _rec(logged))
    assert res == {"session_id": sid, "files": 2, "notes": 2}
    assert logged[-1] == ("INFO", f"session {sid} deleted: 2 files moved back to inbox/{sid}/")
    assert_deleted(lay, sid, shas)
    assert (lay.inbox / sid / "notes.txt").read_text() == (
        "00:00:05–00:00:07  вторая\n01:00:00–01:02:05  a\n    b\n")
    assert (lay.inbox / sid / "start.txt").read_text() == START + "\n"


def test_delete_without_notes_or_start(lay, no_check):
    sid, shas = imported(lay, start=None)
    delete_session(lay, sid, _quiet)
    assert_deleted(lay, sid, shas)
    assert sorted(p.suffix for p in (lay.inbox / sid).iterdir()) == [".WAV", ".WAV"]


@pytest.mark.parametrize("case", ["pending", "tmp", "no_record", "missing_file"])
def test_delete_refuses_incomplete(lay, no_check, case):
    sid, _ = imported(lay)
    sdir = lay.sessions / sid
    if case == "pending":
        os.rename(sdir / "import.json", sdir / "import.pending.json")
    elif case == "tmp":
        (sdir / "originals.tmp").mkdir()
    elif case == "no_record":
        (sdir / "import.json").unlink()
    else:
        next((sdir / "originals").iterdir()).unlink()
    before = sorted(p.name for p in (sdir / "originals").iterdir())
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_incomplete"
    assert sorted(p.name for p in (sdir / "originals").iterdir()) == before
    assert not (lay.inbox / sid).exists() and not (sdir / "delete.pending.json").exists()


@pytest.mark.parametrize("sid", ["../x", "2026-01-01_00-00_1h00m.00000000"])
def test_delete_no_session(lay, sid):
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_no_session"


def test_delete_refuses_foreign_inbox_dir(lay, no_check):
    sid, shas = imported(lay)
    (lay.inbox / f".{sid}.tmp").mkdir()  # not ours: no delete.pending.json
    (lay.inbox / f".{sid}.tmp" / "x.wav").write_bytes(b"chu")
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_inbox_exists"
    assert _shas((lay.sessions / sid / "originals").iterdir()) == shas
    assert not (lay.sessions / sid / "delete.pending.json").exists()


def test_delete_sha_mismatch_rolls_back(lay, no_check, monkeypatch):
    sid, shas = imported(lay)
    real = audio.sha256_file
    monkeypatch.setattr(audio, "sha256_file",
                        lambda p: "0" * 64 if Path(p).parent.name.startswith(".") else real(p))
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_sha_mismatch"
    assert _shas((lay.sessions / sid / "originals").iterdir()) == shas
    assert not (lay.inbox / f".{sid}.tmp").exists() and not (lay.inbox / sid).exists()
    assert [r["session_id"] for r in list_sessions(lay)] == [sid]


@pytest.mark.parametrize("point", ["first_file", "into_inbox", "session_renamed"])
def test_delete_resumes_after_interrupt(lay, no_check, monkeypatch, point):
    sid, shas = imported(lay)
    note(lay, sid, 5.0, 7.5, "заметка")
    real_rename, real_replace = os.rename, os.replace
    calls = []

    def rename(a, b):
        calls.append(a)
        if point == "first_file" and len(calls) == 2:
            raise SystemExit(143)  # SIGTERM between two files
        return real_rename(a, b)

    def replace(a, b):
        real_replace(a, b)
        if (point == "into_inbox" and Path(b) == lay.inbox / sid) or (
                point == "session_renamed" and Path(b) == lay.sessions / f"{sid}.tmp"):
            raise SystemExit(143)

    monkeypatch.setattr(library_mod.os, "rename", rename)
    monkeypatch.setattr(library_mod.os, "replace", replace)
    with pytest.raises(SystemExit):
        delete_session(lay, sid, _quiet)
    monkeypatch.setattr(library_mod.os, "rename", real_rename)
    monkeypatch.setattr(library_mod.os, "replace", real_replace)
    for name in shas:  # every recording is in exactly one place
        where = [p for p in (lay.sessions / sid / "originals" / name,
                             lay.sessions / f"{sid}.tmp" / "originals" / name,
                             lay.inbox / f".{sid}.tmp" / name, lay.inbox / sid / name) if p.exists()]
        assert len(where) == 1, where
    res = delete_session(lay, sid, _quiet)
    assert res == {"session_id": sid, "files": 2, "notes": 1}
    assert_deleted(lay, sid, shas)
    assert (lay.inbox / sid / "notes.txt").read_text() == "00:00:05–00:00:07  заметка\n"


# --- the corrected start comes back with the recordings (D94 p. 4) ---

@pytest.mark.parametrize(("case", "hint"), [
    ("ok", START), ("junk", None), ("big", None), ("symlink", None), ("root", None)])
def test_inventory_start_hint(lay, case, hint, tmp_path):
    folder = lay.inbox if case == "root" else lay.inbox / "s"
    make_folder(folder, chain_starts(2))
    f = folder / "start.txt"
    if case == "symlink":
        (tmp_path / "real.txt").write_text(START)
        f.symlink_to(tmp_path / "real.txt")
    else:
        f.write_text({"junk": "вчера", "big": START + " " * 200}.get(case, START) + "\n")
    chains = inbox_inventory(lay)["sources"][0]["chains"]
    assert [c["start_hint"] for c in chains] == [hint]


def test_reimport_after_delete_same_session_id(lay, no_check):
    sid, shas = imported(lay)
    note(lay, sid, 5.0, 7.5, "заметка")
    delete_session(lay, sid, _quiet)
    chain = next(s for s in inbox_inventory(lay)["sources"] if s["source"] == sid)["chains"][0]
    assert chain["start_hint"] == START
    again = import_originals(lay, sid, 1, chain["start_hint"], _quiet)
    assert again == {"session_id": sid, "existing": False}
    assert not (lay.inbox / sid / "start.txt").exists()
    assert (lay.inbox / sid / "notes.txt").is_file()  # the user's text: never touched
    assert _shas((lay.sessions / sid / "originals").iterdir()) == shas


def test_delete_pending_session_is_not_processable(lay, no_check, monkeypatch):
    """Layer-1 review: a cut delete must not look like a healthy session."""
    sid, _ = imported(lay)
    real = os.rename
    calls = []

    def rename(a, b):
        calls.append(a)
        if len(calls) == 2:
            raise SystemExit(143)
        return real(a, b)

    monkeypatch.setattr(library_mod.os, "rename", rename)
    with pytest.raises(SystemExit):
        delete_session(lay, sid, _quiet)
    row = session_row(lay, sid)
    assert row["state"] == "incomplete" and row["can_process"] is False
    assert row["error"]["code"] == "session_delete_pending"


def test_delete_replaces_stale_empty_gone_dir(lay, no_check):
    sid, shas = imported(lay)
    (lay.sessions / f"{sid}.tmp" / "x.c0.raw").mkdir(parents=True)  # junk of an earlier cut erase
    delete_session(lay, sid, _quiet)
    assert_deleted(lay, sid, shas)


def test_second_delete_keeps_earlier_notes(lay, no_check):
    """Final review: a folder left with only the user's notes does not block the next delete."""
    sid, shas = imported(lay)
    note(lay, sid, 5.0, 7.5, "первая")
    delete_session(lay, sid, _quiet)
    import_originals(lay, sid, 1, START, _quiet)
    make_seg(lay, sid)
    (lay.inbox / sid / "mine.md").write_text("моё")
    delete_session(lay, sid, _quiet)
    assert_deleted(lay, sid, shas)
    names = sorted(p.name for p in (lay.inbox / sid).iterdir() if p.suffix != ".WAV")
    assert names == ["mine.md", "notes-1.txt", "notes.txt", "start.txt"]
    assert (lay.inbox / sid / "notes-1.txt").read_text() == "00:00:05–00:00:07  первая\n"


def test_delete_refuses_inbox_dir_with_wav(lay, no_check):
    sid, _ = imported(lay)
    (lay.inbox / sid).mkdir()
    (lay.inbox / sid / "x.wav").write_bytes(b"")
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_inbox_exists"


def test_delete_cut_inside_erase_finishes(lay, no_check):
    sid, _ = imported(lay)
    delete_session(lay, sid, _quiet)
    gone = lay.sessions / f"{sid}.tmp"  # a cut rmtree: pending already gone, junk left
    (gone / "x.c0.raw").mkdir(parents=True)
    assert delete_session(lay, sid, _quiet) == {"session_id": sid, "files": 0, "notes": 0}
    assert not gone.exists()


def test_second_delete_rollback_returns_carried_notes(lay, no_check, monkeypatch):
    sid, shas = imported(lay)
    note(lay, sid, 5.0, 7.5, "первая")
    delete_session(lay, sid, _quiet)
    import_originals(lay, sid, 1, START, _quiet)
    real = audio.sha256_file
    monkeypatch.setattr(audio, "sha256_file",
                        lambda p: "0" * 64 if Path(p).parent.name.startswith(".") else real(p))
    with pytest.raises(SessionError):
        delete_session(lay, sid, _quiet)
    assert sorted(p.name for p in (lay.inbox / sid).iterdir()) == ["notes.txt"]
    assert _shas((lay.sessions / sid / "originals").iterdir()) == shas


def test_delete_light_stub_erases_dir(lay, no_check):
    """A light backup restored after a delete brings the session back without its recordings."""
    sid, shas = imported(lay)
    delete_session(lay, sid, _quiet)
    before = _shas((lay.inbox / sid).iterdir())
    import_json = lay.sessions / sid / "import.json"
    import_json.parent.mkdir(parents=True)
    import_json.write_text(json.dumps({"session_id": sid, "start": START, "files": [
        {"name": n, "sha256": h} for n, h in shas.items()]}))
    assert delete_session(lay, sid, _quiet) == {"session_id": sid, "files": 0, "notes": 0}
    assert not (lay.sessions / sid).exists() and not (lay.sessions / f"{sid}.tmp").exists()
    assert _shas((lay.inbox / sid).iterdir()) == before


def test_delete_partial_originals_still_incomplete(lay, no_check):
    sid, _ = imported(lay)
    first = min((lay.sessions / sid / "originals").iterdir())
    os.rename(first, lay.inbox / first.name)
    with pytest.raises(SessionError) as e:
        delete_session(lay, sid, _quiet)
    assert e.value.code == "delete_incomplete"
    assert (lay.sessions / sid / "originals").is_dir()


def test_inbox_hint_gives_import_session_id(lay, no_check):
    sid, _ = imported(lay, start="2026-08-11 21:30")
    delete_session(lay, sid, _quiet)
    chain = next(s for s in inbox_inventory(lay)["sources"] if s["source"] == sid)["chains"][0]
    assert chain["session_id"] == sid and chain["imported"] is False


def test_inbox_hint_ignored_for_two_chains(lay):
    folder = lay.inbox / "s"
    make_folder(folder, chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    (folder / "start.txt").write_text(START + "\n")
    chains = inbox_inventory(lay)["sources"][0]["chains"]
    assert [c["start_hint"] for c in chains] == [None, None]


def test_sweep_erases_leftover_gone_dir(lay, no_check):
    sid, _ = imported(lay)
    delete_session(lay, sid, _quiet)
    (lay.sessions / f"{sid}.tmp" / "x.c0.raw").mkdir(parents=True)  # a cut erase, its job lost
    assert sweep_deleted(lay) == ([sid], [])
    assert not (lay.sessions / f"{sid}.tmp").exists()


def test_sweep_keeps_dir_with_originals(lay):
    gone = lay.sessions / "2025-03-01_09-00_0h00m.00000000.tmp"
    (gone / "originals").mkdir(parents=True)
    (gone / "originals" / "a.WAV").write_bytes(b"RIFF")
    erased, said = sweep_deleted(lay)
    assert (gone / "originals" / "a.WAV").is_file() and erased == [] and "a.WAV" in said[0]


def test_sweep_ignores_live_session_and_other_names(lay, no_check):
    sid, _ = imported(lay)
    (lay.sessions / f"{sid}.tmp").mkdir()  # the session dir is still there: a delete may be running
    (lay.sessions / "notes.tmp").mkdir()
    assert sweep_deleted(lay) == ([], [])
    assert (lay.sessions / f"{sid}.tmp").is_dir() and (lay.sessions / "notes.tmp").is_dir()


def test_sweep_skips_a_session_with_a_live_job(lay):
    gone = lay.sessions / "2025-03-01_09-00_0h00m.00000000.tmp"
    (gone / "x").mkdir(parents=True)
    assert sweep_deleted(lay, busy=lambda sid: True) == ([], [])
    assert gone.is_dir()


def test_inbox_hint_replan_failure_keeps_the_folder(lay, monkeypatch):
    folder = lay.inbox / "s"
    make_folder(folder, chain_starts(2))
    (folder / "start.txt").write_text(START + "\n")
    real = library_mod.audio.plan_session

    def plan(*a, start=None, **k):
        if start is not None:
            raise OSError("gone")  # the files changed between the two header passes
        return real(*a, **k)
    monkeypatch.setattr(library_mod.audio, "plan_session", plan)
    src = inbox_inventory(lay)["sources"][0]
    assert src["error"] is None and [c["start_hint"] for c in src["chains"]] == [None]
