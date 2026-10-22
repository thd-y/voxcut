import json
import os
from datetime import timedelta

import pytest
from test_session import T0, chain_starts, make_folder

from voxcut import audio
from voxcut.audio import RunError, SourceError, sha256_file
from voxcut.intervals import SessionError
from voxcut.library import (
    Layout,
    clip_table,
    finish_import,
    import_originals,
    inbox_inventory,
    list_sessions,
    process_session,
    resolve_clip,
    session_runs,
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
    assert "run.json" in got[SID2]["error"]


# --- import from inbox/ (UI task 5, U4, D39) ---

@pytest.fixture
def no_check(monkeypatch):
    """check_source decodes with ffmpeg (D18); the slow test runs it for real."""
    monkeypatch.setattr(audio, "check_source", lambda p: None)


def _shas(paths):
    return {p.name: sha256_file(str(p)) for p in paths}


def _quiet(_msg):
    pass


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
    import_originals(lay, "13_oct", 1, None, msgs.append)
    assert {p.name: p.read_bytes() for p in (lay.inbox / "13_oct").iterdir()} == extra
    warn = [m for m in msgs if m.startswith("в inbox/")]
    assert warn == [("в inbox/13_oct/ остались файлы: desktop.ini, x.wav:Zone.Identifier; "
                     "папка скрыта, удалите вручную")]


def test_import_rmdir_race_is_not_failure(lay, no_check, monkeypatch):
    make_folder(lay.inbox / "13_oct", chain_starts(2))

    def busy(path, *a, **kw):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(os, "rmdir", busy)
    msgs = []
    res = import_originals(lay, "13_oct", 1, None, msgs.append)
    assert res["existing"] is False
    assert any(m.startswith("в inbox/13_oct/ остались файлы") for m in msgs)


def test_import_second_chain_left_without_warning(lay, no_check):
    make_folder(lay.inbox / "13_oct", chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    msgs = []
    import_originals(lay, "13_oct", 1, None, msgs.append)
    left = sorted(p.name for p in (lay.inbox / "13_oct").iterdir())
    assert len(left) == 1 and left[0].lower().endswith(".wav")  # chain 2 is still there
    assert not any(m.startswith("в inbox/") for m in msgs)  # never «удалите вручную» on a WAV
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
    with pytest.raises(SessionError, match="ссылка"):
        import_originals(lay, "", 1, None, _quiet)
    assert paths[0].is_file() and paths[1].is_symlink() and list(lay.sessions.iterdir()) == []


def test_import_inbox_changed_between_reads_refused(lay, no_check, monkeypatch):
    make_folder(lay.inbox, chain_starts(2))
    monkeypatch.setattr(audio, "list_chains", lambda src: [["20250301082921.WAV"]])  # stale read
    with pytest.raises(SessionError, match="входящие изменились"):
        import_originals(lay, "", 1, None, _quiet, files=["20250301082921.WAV"])
    assert list(lay.sessions.iterdir()) == []


def test_import_repeat_of_gone_chain_refused(lay, no_check):
    make_folder(lay.inbox, chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    c1 = inbox_inventory(lay)["sources"][0]["chains"][0]["files"]
    import_originals(lay, "", 1, None, _quiet, files=c1)
    left = sorted(p.name for p in lay.inbox.iterdir())
    with pytest.raises(SessionError, match="нет во входящих"):
        import_originals(lay, "", 1, "2026-08-11 21:30", _quiet, files=c1)  # double click
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
    with pytest.raises(SessionError, match="источник"):
        import_originals(lay, source, 1, None, _quiet)


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
    got = import_originals(lay, "", 1, None, logged.append)
    assert (lay.sessions / got["session_id"] / "import.json").is_file()
    assert any(SID2 in m for m in logged)
    assert not any("junk" in m for m in logged)


def test_import_partial_overlap_refused(lay, no_check):
    paths = make_folder(lay.inbox, chain_starts(2))
    keep = paths[0].read_bytes()
    import_originals(lay, "", 1, None, _quiet)
    make_folder(lay.inbox, chain_starts(2), seed=7)
    (lay.inbox / paths[0].name).write_bytes(keep)
    with pytest.raises(SessionError, match=paths[0].name):
        import_originals(lay, "", 1, None, _quiet)
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
    with pytest.raises(SessionError, match="sha256"):
        import_originals(lay, "", 1, None, _quiet)
    sid = next(lay.sessions.iterdir()).name
    assert sorted(p.name for p in (lay.sessions / sid / "originals").iterdir()) == sorted(
        p.name for p in paths)
    assert list_sessions(lay)[0]["state"] == "incomplete"


def test_import_unfinished_tmp_refused(lay, no_check):
    make_folder(lay.inbox, chain_starts(2))
    sid = inbox_inventory(lay)["sources"][0]["chains"][0]["session_id"]
    (lay.sessions / sid / "originals.tmp").mkdir(parents=True)
    with pytest.raises(SessionError, match="originals.tmp"):
        import_originals(lay, "", 1, None, _quiet)
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
    assert "склейка пауз" in src["broken"]["error"] and src["broken"]["chains"] == []
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
    with pytest.raises(RunError, match="light"):
        process_session(lay, sid, lambda s: None, _quiet)


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
    with pytest.raises(RunError, match="нет сессии"):
        process_session(lay, sid, lambda s: None, _quiet)


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
    with pytest.raises(SessionError, match="во входящих"):
        import_originals(lay, "", 1, None, _quiet)
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
    with pytest.raises(SessionError, match="во входящих"):
        import_originals(lay, "", 1, None, _quiet)
    assert _shas(paths) == before
    assert not any(lay.sessions.iterdir())


def test_import_rollback_failure_keeps_pending(lay, no_check, monkeypatch):
    paths = make_folder(lay.inbox, chain_starts(3))
    before = _shas(paths)
    _rename_failing(monkeypatch, 2, fail_back=True)
    with pytest.raises(SessionError, match="originals.tmp"):
        import_originals(lay, "", 1, None, _quiet)
    monkeypatch.undo()
    sdir = next(lay.sessions.iterdir())
    moved = sorted(p.name for p in (sdir / "originals.tmp").iterdir())
    assert moved == [paths[0].name]
    assert (sdir / "import.pending.json").is_file()
    after = {p.name: sha256_file(str(p)) for p in [*paths[1:], sdir / "originals.tmp" / moved[0]]}
    assert after == before


def test_import_sigterm_mid_move_left_for_process(lay, no_check, monkeypatch):
    """SystemExit is not rolled back: the pending import stays for «Обработать» (finish_import)."""
    paths = make_folder(lay.inbox, chain_starts(3))
    real, n = os.rename, [0]

    def rename(src, dst):
        n[0] += 1
        if n[0] == 2:
            raise SystemExit(143)
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(SystemExit):
        import_originals(lay, "", 1, None, _quiet)
    monkeypatch.undo()
    sdir = next(lay.sessions.iterdir())
    assert (sdir / "import.pending.json").is_file()
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
    with pytest.raises(SessionError, match="нет в originals"):
        finish_import(lay, sid, _quiet)
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
    assert "не удаляйте" in row["error"]
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
    with pytest.raises(SessionError, match="sha256"):
        finish_import(lay, sid, _quiet)
    assert not (lay.sessions / sid / "import.json").exists()
