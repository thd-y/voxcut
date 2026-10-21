import json
import os
import threading
import uuid

import numpy as np
import pytest
import soundfile as sf
from test_server_security import SECURITY, Client

from voxcut.journal import Journal
from voxcut.library import Layout
from voxcut.ui import server as server_mod
from voxcut.ui.server import make_server

SID = "2026-08-11_21-30_4h16m.0badface"


def build_session(lay, sid=SID, n=3):
    """A processed session made by hand: import.json, session.json, one seg run with clips."""
    s = lay.sessions / sid
    (s / "originals").mkdir(parents=True)
    (s / "import.json").write_text("{}")
    sess = s / f"{sid}.session"
    sess.mkdir()
    (sess / "session.json").write_text(json.dumps({
        "wav": "x.wav", "n_samples": 16000 * 60, "sr": 16000,
        "files": [{"start_local": "2026-08-11T21:30:00"}]}))
    seg = s / "x.c0.raw" / "seg-aaaaaaaa"
    (seg / "clips").mkdir(parents=True)
    (seg / "run.json").write_text(json.dumps({"meta": {"name": "v1"}}))
    rows = []
    for i in range(n):
        name = f"clips/{i:04d}.wav"
        sf.write(seg / name, np.full((1600, 1), i, np.int16), 16000)
        rows.append({"start_s": 10.0 * i, "end_s": 10.0 * i + 5, "duration_s": 5.0,
                     "p_max": 0.5 + i / 10, "start_local": f"2026-08-11T21:30:{i:02d}",
                     "audio_path": name, "source_path": "/secret/path.wav"})
    (seg / "clips" / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return seg


@pytest.fixture
def api(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    seg = build_session(lay)
    server, app = make_server(lay)
    app.log = lambda line: None
    th = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    th.start()
    c = Client(app)
    c.lay, c.seg, c.k = lay, seg, c.key()
    yield c
    server.shutdown()
    server.server_close()


def get(c, path, key=True, **kw):
    status, headers, body = c.req("GET", path, key=c.k if key else None, **kw)
    return status, headers, body


def session(c, sid=SID, query=""):
    status, _, body = get(c, f"/api/session/{sid}{query}")
    assert status == 200
    return json.loads(body)


def test_state_shape(api):
    status, headers, body = get(api, "/api/state")
    assert status == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    st = json.loads(body)
    assert set(st) == {"sessions", "inbox", "jobs", "last_backup", "ideas", "journal_warnings"}
    assert [s["session_id"] for s in st["sessions"]] == [SID]
    assert st["inbox"] == {"sources": [], "backups": []} and st["jobs"] == []
    assert st["last_backup"] is None and st["ideas"] == [] and st["journal_warnings"] == []


def test_session_view(api):
    v = session(api)
    assert v["session_id"] == SID and v["start_local"] == "2026-08-11T21:30:00"
    assert v["duration_s"] == 60.0 and len(v["runs"]) == 1
    assert v["run_id"] == v["runs"][0]["run_id"] and set(v["runs"][0]) == {
        "run_id", "name", "created"}
    c0 = v["clips"][0]
    assert set(c0) == {"clip_id", "n", "start_s", "end_s", "start_local", "duration_s",
                       "p_max", "marks", "notes", "heard"}
    assert "secret" not in json.dumps(v)  # no paths reach the client
    assert session(api, query=f"?run={v['run_id']}") == v
    assert get(api, f"/api/session/{SID}?run=ffffffffffff")[0] == 404


def test_session_view_marks(api):
    Journal(api.lay.journal).append({"event_id": uuid.uuid4().hex, "kind": "mark",
                                     "value": "моё", "session_id": SID,
                                     "start_s": 9.0, "end_s": 12.0})
    clips = session(api)["clips"]
    assert [c["marks"] for c in clips] == [[], ["моё"], []]


def test_clip_bytes_with_key(api):
    clip = session(api)["clips"][1]
    status, headers, body = get(api, f"/api/clip/{clip['clip_id']}")
    assert status == 200 and headers["Content-Type"] == "audio/wav"
    assert body == (api.seg / "clips" / "0001.wav").read_bytes()
    assert {k: headers.get(k) for k in SECURITY} == SECURITY


def test_clip_no_key_401_before_open(api, monkeypatch):
    clip = session(api)["clips"][0]

    def fail(*a, **k):
        raise AssertionError("файл открыт до проверки ключа")

    monkeypatch.setattr(server_mod, "open", fail, raising=False)
    assert get(api, f"/api/clip/{clip['clip_id']}", key=False)[0] == 401
    assert api.req("GET", f"/api/clip/{clip['clip_id']}",
                   headers={"Authorization": "Bearer nope"})[0] == 401


def test_clip_head_405(api):
    clip = session(api)["clips"][0]
    assert api.req("HEAD", f"/api/clip/{clip['clip_id']}", key=api.k)[0] == 405
    assert api.req("HEAD", f"/api/clip/{clip['clip_id']}")[0] == 401


@pytest.mark.parametrize("case", ["dotdot", "absolute", "symlink"])
def test_clip_manifest_escape_404(api, case):
    outside = api.lay.root / "outside.wav"
    outside.write_bytes(b"RIFF secret")
    m = api.seg / "clips" / "manifest.jsonl"
    rows = [json.loads(x) for x in m.read_text().splitlines()]
    if case == "symlink":
        (api.seg / "clips" / "0002.wav").unlink()
        os.symlink(outside, api.seg / "clips" / "0002.wav")
    else:
        rows[2]["audio_path"] = "../../../../../outside.wav" if case == "dotdot" else str(outside)
    m.write_text("".join(json.dumps(r) + "\n" for r in rows))
    clip = session(api)["clips"][2]
    status, _, body = get(api, f"/api/clip/{clip['clip_id']}")
    assert status == 404 and b"secret" not in body


@pytest.mark.parametrize("sid", ["..%2F..%2Fetc", "../../etc", SID[:-1] + "0",
                                 "2026-08-11_21-30_4h16m.00000000"])
def test_unknown_session_404(api, sid):
    assert get(api, f"/api/session/{sid}")[0] == 404


def test_unknown_clip_404(api):
    assert get(api, "/api/clip/" + "0" * 16)[0] == 404


def test_range_ignored(api):
    clip = session(api)["clips"][0]
    status, headers, body = get(api, f"/api/clip/{clip['clip_id']}",
                                headers={"Range": "bytes=0-9"})
    assert status == 200 and len(body) == (api.seg / "clips" / "0000.wav").stat().st_size
    assert "Content-Range" not in headers
