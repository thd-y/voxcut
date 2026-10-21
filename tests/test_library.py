import json
import os

import pytest

from voxcut.library import (
    Layout,
    clip_table,
    list_sessions,
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
