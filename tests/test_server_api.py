import json
import os
import threading
import uuid

import numpy as np
import pytest
import soundfile as sf
from test_jobs import FakeRun
from test_server_security import SECURITY, Client

from voxcut import journal as journal_mod
from voxcut import library as library_mod
from voxcut.jobs import Jobs
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
                       "p_max", "marks", "notes", "heard", "own_mark"}
    assert "secret" not in json.dumps(v)  # no paths reach the client
    assert session(api, query=f"?run={v['run_id']}") == v
    assert get(api, f"/api/session/{SID}?run=ffffffffffff")[0] == 404


def test_session_view_marks(api):
    Journal(api.lay.journal).append({"event_id": uuid.uuid4().hex, "kind": "mark",
                                     "value": "моё", "session_id": SID,
                                     "start_s": 9.0, "end_s": 12.0})
    clips = session(api)["clips"]
    assert [c["marks"] for c in clips] == [[], ["моё"], []]


def test_broken_session_409_with_reason(api):
    """Review #5: a broken manifest says why instead of «внутренняя ошибка сервера»."""
    (api.seg / "clips" / "manifest.jsonl").write_text('{"start_s": 0, "end_s": 5}\n{oops\n')
    status, _, body = get(api, f"/api/session/{SID}")
    assert status == 409 and "JSONDecodeError" in json.loads(body)["error"]


def test_session_view_parses_journal_once(api, monkeypatch):
    """Review #2: the view cost is events + clips, not events × clips."""
    j = Journal(api.lay.journal)
    for i in range(10):
        j.append({"event_id": uuid.uuid4().hex, "kind": "heard", "value": "",
                  "session_id": SID, "start_s": float(i), "end_s": i + 1.0})
    calls = []
    real = journal_mod._when
    monkeypatch.setattr(journal_mod, "_when", lambda e: calls.append(1) or real(e))
    assert session(api)["clips"][0]["heard"] is True
    assert len(calls) <= 10  # once per event, whatever the number of clips (3 here)


def test_session_view_reads_one_row(api, monkeypatch):
    def boom(lay):
        raise AssertionError("list_sessions for one session")

    monkeypatch.setattr(library_mod, "list_sessions", boom)
    assert session(api)["session_id"] == SID


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


# --- write API: journal and jobs (UI task 11) ---

@pytest.fixture
def wapi(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    build_session(lay)
    run = FakeRun()
    jobs = Jobs(lay, run=run)
    server, app = make_server(lay, jobs=jobs)
    logs = []
    app.log = logs.append
    th = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    th.start()
    c = Client(app)
    c.lay, c.k, c.run, c.jobs, c.logs = lay, c.key(), run, jobs, logs
    c.clips = session(c)["clips"]
    yield c
    server.shutdown()
    server.server_close()


def post(c, path, body, key=None, **kw):
    status, headers, raw = c.req("POST", path, body, key=key or c.k, **kw)
    return status, headers, json.loads(raw) if raw else None


def mark(c, value="моё", clip=0, **extra):
    return {"event_id": uuid.uuid4().hex, "kind": "mark", "value": value,
            "clip_id": c.clips[clip]["clip_id"]} | extra


def test_journal_post_saved_after_fsync(wapi, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(journal_mod.os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    status, _, body = post(wapi, "/api/journal", mark(wapi))
    assert status == 200 and body["saved"] is True and calls
    ev = body["event"]
    assert (ev["session_id"], ev["start_s"], ev["end_s"]) == (SID, 0.0, 5.0)
    assert session(wapi)["clips"][0]["marks"] == ["моё"]


def test_journal_post_idempotent(wapi):
    e = mark(wapi)
    first = post(wapi, "/api/journal", e)[2]["event"]
    assert post(wapi, "/api/journal", e)[2]["event"] == first
    assert len(wapi.lay.journal.read_text().splitlines()) == 1


@pytest.mark.parametrize("case", ["unknown_clip", "long_mark", "path_key", "heard_value",
                                  "idea_unknown_session", "backup_kind", "no_clip",
                                  "bad_event_id", "session_on_mark", "long_note"])
def test_journal_rejects(wapi, case):
    e = {
        "unknown_clip": mark(wapi) | {"clip_id": "0" * 16},
        "long_mark": mark(wapi, value="x" * 33),
        "path_key": mark(wapi, path="/etc/passwd"),
        "heard_value": mark(wapi, value="x") | {"kind": "heard"},
        "idea_unknown_session": {"event_id": uuid.uuid4().hex, "kind": "idea", "value": "i",
                                 "session_id": "2026-08-11_21-30_4h16m.00000000"},
        "backup_kind": {"event_id": uuid.uuid4().hex, "kind": "backup", "value": "x"},
        "no_clip": {"event_id": uuid.uuid4().hex, "kind": "mark", "value": "моё"},
        "bad_event_id": mark(wapi) | {"event_id": "../x"},
        "session_on_mark": mark(wapi, session_id=SID),
        "long_note": mark(wapi, value="x" * 10_001) | {"kind": "note"},
    }[case]
    assert post(wapi, "/api/journal", e)[0] == 400
    assert not wapi.lay.journal.exists() or wapi.lay.journal.read_text() == ""


def test_idea_with_and_without_session(wapi):
    for sid in (None, SID):
        e = {"event_id": uuid.uuid4().hex, "kind": "idea", "value": f"идея {sid}"}
        assert post(wapi, "/api/journal", e | ({"session_id": sid} if sid else {}))[0] == 200
    st = json.loads(get(wapi, "/api/state")[2])
    assert [i["value"] for i in st["ideas"]] == ["идея None", f"идея {SID}"]


def test_idea_and_process_read_one_row(wapi, monkeypatch):
    def boom(lay):
        raise AssertionError("list_sessions for one session")

    monkeypatch.setattr(library_mod, "list_sessions", boom)
    e = {"event_id": uuid.uuid4().hex, "kind": "idea", "value": "x", "session_id": SID}
    assert post(wapi, "/api/journal", e)[0] == 200
    assert post(wapi, "/api/journal", e | {"event_id": uuid.uuid4().hex,
                                          "session_id": SID[:-1] + "0"})[0] == 400
    status, _, body = post(wapi, "/api/jobs", {"job_id": uuid.uuid4().hex, "kind": "process",
                                               "params": {"session_id": SID}})
    assert status == 200 and body["job"]["state"] == "running"


def test_journal_requires_origin(wapi):
    status, _, _ = wapi.req("POST", "/api/journal", mark(wapi), key=wapi.k, origin=False,
                            headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 403
    status, _, _ = wapi.req("POST", "/api/journal", mark(wapi), key=wapi.k, origin=False,
                            headers={"Origin": wapi.origin, "Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert not wapi.lay.journal.exists()


def test_malicious_note_returned_as_json_text(wapi):
    evil = '<img src=x onerror=alert(1)></script><script>alert(2)</script>'
    e = mark(wapi, value=evil) | {"kind": "note"}
    assert post(wapi, "/api/journal", e)[0] == 200
    _, headers, raw = get(wapi, f"/api/session/{SID}")
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert json.loads(raw)["clips"][0]["notes"][0]["value"] == evil


def test_jobs_post_validated(wapi):
    jid = uuid.uuid4().hex
    for params in ({"source": "../data", "chain": 1, "files": ["a.wav"], "start": None},
                   {"source": "/etc", "chain": 1, "files": ["a.wav"], "start": None}):
        assert post(wapi, "/api/jobs", {"job_id": jid, "kind": "import", "params": params})[0] \
            == 400
    assert post(wapi, "/api/jobs", {"job_id": jid, "kind": "shell", "params": {}})[0] == 400
    assert post(wapi, "/api/jobs", {"job_id": jid, "kind": "backup", "params": {"mode": "full"},
                                    "unit": "x"})[0] == 400
    assert wapi.run.started() == []
    status, _, body = post(wapi, "/api/jobs", {"job_id": jid, "kind": "backup",
                                               "params": {"mode": "full"}})
    assert status == 200 and body["job"]["state"] == "running"
    status, _, body = post(wapi, f"/api/jobs/{jid}/cancel", {})
    assert status == 200 and body["job"]["state"] == "cancelled"
    assert post(wapi, f"/api/jobs/{uuid.uuid4().hex}/cancel", {})[0] == 404


def test_backup_done_appends_event_once(wapi):
    jid = uuid.uuid4().hex
    post(wapi, "/api/jobs", {"job_id": jid, "kind": "backup", "params": {"mode": "light"}})
    (wapi.lay.jobs / jid / "status.json").write_text(json.dumps(
        {"state": "done", "result": "voxcut-backup_2026-10-21_10-00_light.tar"}))
    wapi.jobs.poll()
    wapi.app.on_done(wapi.jobs.list()[0])  # a repeat after a crash must not duplicate
    events, _ = journal_mod.load(wapi.lay.journal)
    assert [(e["kind"], e["value"]) for e in events] == [
        ("backup", "voxcut-backup_2026-10-21_10-00_light.tar")]
    assert json.loads(get(wapi, "/api/state")[2])["last_backup"] == events[0]["ts"]


def test_restore_merges_journal_once(wapi):
    old = Journal(wapi.lay.root / "old.jsonl")
    old.append({"event_id": "c" * 32, "kind": "idea", "value": "из бэкапа"})
    (wapi.lay.inbox / "voxcut-backup_x_full.tar").write_bytes(b"")
    jid = uuid.uuid4().hex
    post(wapi, "/api/jobs", {"job_id": jid, "kind": "restore",
                             "params": {"name": "voxcut-backup_x_full.tar"}})
    (wapi.lay.jobs / jid / "journal.jsonl").write_bytes((wapi.lay.root / "old.jsonl").read_bytes())
    (wapi.lay.jobs / jid / "status.json").write_text(json.dumps({"state": "done"}))
    wapi.jobs.poll()
    wapi.jobs.poll()
    wapi.app.on_done(wapi.jobs.list()[0])
    events, _ = journal_mod.load(wapi.lay.journal)
    assert [e["event_id"] for e in events] == ["c" * 32]
    assert events[0]["ts"] == old.events()[0]["ts"]


@pytest.mark.parametrize("case, advice", [
    ("transient", "восстановите этот архив ещё раз"),  # review #4: on_done runs once
    ("broken", "повтор не поможет"),  # the archive's journal itself: retrying loops
])
def test_restore_merge_failure_advice(wapi, monkeypatch, case, advice):
    (wapi.lay.inbox / "voxcut-backup_x_full.tar").write_bytes(b"")
    jid = uuid.uuid4().hex
    post(wapi, "/api/jobs", {"job_id": jid, "kind": "restore",
                             "params": {"name": "voxcut-backup_x_full.tar"}})
    if case == "broken":
        (wapi.lay.jobs / jid / "journal.jsonl").write_bytes(b"{broken\n{}\n")
    else:
        (wapi.lay.jobs / jid / "journal.jsonl").write_bytes(b"")

        def full(events):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(wapi.app.journal, "merge", full)
    (wapi.lay.jobs / jid / "status.json").write_text(json.dumps({"state": "done"}))
    wapi.jobs.poll()
    wapi.jobs.poll()
    job = next(j for j in wapi.jobs.list() if j["job_id"] == jid)
    assert advice in job["error"] and job["state"] == "failed"  # the badge shows «failed»


def test_get_state_does_not_start_jobs(wapi):
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    post(wapi, "/api/jobs", {"job_id": a, "kind": "backup", "params": {"mode": "light"}})
    post(wapi, "/api/jobs", {"job_id": b, "kind": "backup", "params": {"mode": "light"}})
    (wapi.lay.jobs / a / "status.json").write_text(json.dumps({"state": "done"}))
    n = len(wapi.run.calls)
    st = json.loads(get(wapi, "/api/state")[2])
    assert len(wapi.run.calls) == n
    assert {j["job_id"]: j["state"] for j in st["jobs"]}[b] == "queued"


def test_note_text_not_in_log(wapi):
    e = mark(wapi, value="личная заметка 12345") | {"kind": "note"}
    post(wapi, "/api/journal", e)
    text = "\n".join(wapi.logs)
    assert "POST /api/journal 200" in text
    assert "личная" not in text and e["event_id"] not in text and e["clip_id"] not in text


def test_two_clients_concurrent_marks(wapi):
    keys = [wapi.k, wapi.key()]
    errors = []

    def work(key):
        for i in range(50):
            status, _, _ = post(wapi, "/api/journal", mark(wapi, clip=i % 3), key=key)
            if status != 200:
                errors.append(status)

    ts = [threading.Thread(target=work, args=(k,)) for k in keys]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    events, warnings = journal_mod.load(wapi.lay.journal)
    assert errors == [] and len(events) == 100 and warnings == []


def test_aborted_clip_download_logs_template_only(api, capfd):
    """AbortController on ←/→ drops the connection mid-body: no traceback, no path (S8)."""
    import socket as socket_mod
    import time

    seg = api.seg
    sf.write(seg / "clips" / "0000.wav", np.zeros((16000 * 120, 2), np.int16), 16000)
    clip = session(api)["clips"][0]
    logs = []
    api.app.log = logs.append
    s = socket_mod.create_connection(("127.0.0.1", api.port))
    s.sendall((f"GET /api/clip/{clip['clip_id']} HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\n"
               f"Authorization: Bearer {api.k}\r\n\r\n").encode())
    s.recv(4096)
    s.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.close()  # RST mid-body
    time.sleep(0.5)
    err = capfd.readouterr().err
    assert "Traceback" not in err and str(seg) not in err
    assert all(line.startswith("GET /api/clip/{clip_id} ") for line in logs), logs


def test_clip_vanished_404_without_path(api, monkeypatch):
    clip = session(api)["clips"][0]
    logs = []
    api.app.log = logs.append
    (api.seg / "clips" / "0000.wav").unlink()
    status, _, body = get(api, f"/api/clip/{clip['clip_id']}")
    assert status == 404 and str(api.seg).encode() not in body
    assert "0000" not in "\n".join(logs)


def test_stream_error_logged_by_type_only(api, capfd, tmp_path):
    p = tmp_path / "secret-dir" / "x.wav"
    p.parent.mkdir()
    p.write_bytes(b"x" * 100)

    class BadFile:
        def __init__(self):
            self.f = open(p, "rb")  # noqa: SIM115 - closed in __exit__

        def fileno(self):
            return self.f.fileno()

        def read(self, n=-1):
            raise OSError(f"I/O error reading {p}")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    api.app.route("GET", "/api/boom")(lambda req: server_mod.Response(200, BadFile()))
    logs = []
    api.app.log = logs.append
    try:
        get(api, "/api/boom")
    except Exception:  # noqa: BLE001, S110 - the connection may be cut; only the log matters
        pass
    import time
    time.sleep(0.3)
    err = capfd.readouterr().err
    assert "secret-dir" not in err and "Traceback" not in err
    assert "secret-dir" not in "\n".join(logs) and any("OSError" in x for x in logs)


# --- D49: open inbox/ in Explorer ---

@pytest.fixture
def oapi(wapi):
    calls = []
    wapi.app.open_folder = calls.append
    wapi.opened = calls
    return wapi


def test_inbox_open_calls_explorer_with_fixed_path(oapi):
    assert post(oapi, "/api/inbox/open", {})[::2] == (200, {"ok": True})
    assert oapi.opened == [oapi.lay.inbox]


def test_inbox_open_rejects_body_fields(oapi):
    assert post(oapi, "/api/inbox/open", {"path": "/etc"})[0] == 400
    assert oapi.opened == []


def test_inbox_open_without_key_is_401(oapi):
    assert oapi.req("POST", "/api/inbox/open", {})[0] == 401
    status, _, _ = oapi.req("POST", "/api/inbox/open", {}, key=oapi.k, origin=False,
                            headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 403
    assert oapi.opened == []


def test_inbox_open_tool_missing_is_503(oapi):
    def missing(path):
        raise FileNotFoundError("wslpath")

    oapi.app.open_folder = missing
    status, _, body = post(oapi, "/api/inbox/open", {})
    assert status == 503 and "проводник недоступен" in body["error"]


def test_open_folder_argv(tmp_path):
    runs, popens = [], []

    def run(argv, **kw):
        runs.append(argv)
        return type("R", (), {"stdout": "\\\\wsl.localhost\\U\\inbox\n"})()

    def popen(argv, **kw):
        popens.append((argv, kw))

    target = tmp_path / "inbox"
    server_mod.open_folder(target, run=run, popen=popen)
    assert target.is_dir()
    assert runs == [["wslpath", "-w", str(target)]]
    argv, kw = popens[0]
    assert argv == ["explorer.exe", "\\\\wsl.localhost\\U\\inbox"]
    assert kw["start_new_session"] is True and "shell" not in kw


# --- D56: delete an idea ---

def idea(c, value="идея"):
    e = {"event_id": uuid.uuid4().hex, "kind": "idea", "value": value}
    assert post(c, "/api/journal", e)[0] == 200
    return e["event_id"]


def lines(c):
    return c.lay.journal.read_text().splitlines()


def test_idea_delete_hides_from_state(wapi):
    keep, gone = idea(wapi, "остаётся"), idea(wapi, "уходит")
    d = {"event_id": uuid.uuid4().hex, "kind": "idea_deleted", "value": gone}
    assert post(wapi, "/api/journal", d)[0] == 200
    assert post(wapi, "/api/journal", d)[0] == 200  # the same event_id again: idempotent
    st = json.loads(get(wapi, "/api/state")[2])
    assert [i["event_id"] for i in st["ideas"]] == [keep]
    assert sum('"idea_deleted"' in line for line in lines(wapi)) == 1


@pytest.mark.parametrize("case", ["mark", "unknown", "extra_field"])
def test_idea_delete_refused(wapi, case):
    m = mark(wapi)
    assert post(wapi, "/api/journal", m)[0] == 200
    target = {"mark": m["event_id"], "unknown": uuid.uuid4().hex}.get(case, idea(wapi))
    d = {"event_id": uuid.uuid4().hex, "kind": "idea_deleted", "value": target}
    if case == "extra_field":
        d["session_id"] = "x"
    before = len(lines(wapi))
    assert post(wapi, "/api/journal", d)[0] == 400
    assert len(lines(wapi)) == before
