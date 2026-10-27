import json
import os
import subprocess
import sys
import uuid

import pytest

from voxcut import jobs
from voxcut.jobs import LIST_MAX, JobError, JobParamsError, Jobs, ensure_slice, unit_argv, validate
from voxcut.library import Layout

SID = "2026-08-11_21-30_4h16m.0badface"


class FakeRun:
    """Stands in for subprocess.run: records argv, answers systemctl show / is-active."""

    def __init__(self, show="MemoryMax=5368709120\nMemorySwapMax=0\n", active="active"):
        self.calls, self.show, self.active = [], show, active

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = ""
        if argv[:3] == ["systemctl", "--user", "show"]:
            out = self.show
        elif argv[:3] == ["systemctl", "--user", "is-active"]:
            out = self.active + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def started(self):
        return [a for a in self.calls if a[0] == "systemd-run"]


@pytest.fixture
def lay(tmp_path):
    lay = Layout(tmp_path)
    lay.ensure()
    return lay


def jid():
    return uuid.uuid4().hex


def finish(lay, job_id, state="done"):
    (lay.jobs / job_id / "status.json").write_text(json.dumps({"state": state, "stage": None,
                                                               "error": None}))


def test_list_keeps_last_finished_and_every_active(lay):
    """Review #6: /api/state polls this every 2 s; .run/jobs/ only grows."""
    run = FakeRun()
    j = Jobs(lay, run=run)
    ids = [jid() for _ in range(LIST_MAX + 5)]
    for i in ids:
        j.submit(i, "backup", {"mode": "light"})
        finish(lay, i)
        j.poll()
    late = jid()
    j.submit(late, "backup", {"mode": "light"})
    view = [v["job_id"] for v in Jobs(lay, run=run).list()]  # also after a server restart
    assert view == ids[-(LIST_MAX - 1):] + [late]


def test_list_never_hides_running_job(lay):
    j = Jobs(lay, run=FakeRun())
    running = jid()
    j.submit(running, "backup", {"mode": "light"})
    for _ in range(LIST_MAX):
        j.submit(jid(), "backup", {"mode": "light"})  # queued behind it
    view = j.list()
    assert len(view) == LIST_MAX + 1 and view[0]["job_id"] == running


def test_unit_argv_exact(tmp_path):
    assert unit_argv("voxcut-job-x", ["py", "-m", "voxcut.jobs", "d"], tmp_path) == [
        "systemd-run", "--user", "--quiet", "--collect", "--unit=voxcut-job-x",
        "--slice=voxcut.slice",
        "-p", "MemoryMax=5G", "-p", "MemorySwapMax=0", "-p", "PrivateNetwork=yes",
        "-p", "UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy",
        "--setenv=HF_HUB_OFFLINE=1", "--setenv=TRANSFORMERS_OFFLINE=1",
        f"--working-directory={tmp_path}", "--", "py", "-m", "voxcut.jobs", "d"]


def test_ensure_slice_refuses_when_not_applied(lay):
    run = FakeRun(show="MemoryMax=infinity\nMemorySwapMax=infinity\n")
    with pytest.raises(JobError, match="memory limit") as e:
        ensure_slice(run)
    assert e.value.code == "job_slice_limit"
    j = Jobs(lay, run=run)
    view = j.submit(jid(), "backup", {"mode": "light"})
    assert view["state"] == "failed" and view["error"]["code"] == "job_slice_limit"
    assert run.started() == []


def test_one_heavy_at_a_time(lay):
    run, done = FakeRun(), []
    j = Jobs(lay, run=run, on_done=done.append)
    a, b = jid(), jid()
    assert j.submit(a, "backup", {"mode": "light"})["state"] == "running"
    assert j.submit(b, "backup", {"mode": "full"})["state"] == "queued"
    assert len(run.started()) == 1
    j.poll()
    assert len(run.started()) == 1
    finish(lay, a)
    assert j.poll() is None
    states = {v["job_id"]: v["state"] for v in j.list()}
    assert states == {a: "done", b: "running"}
    assert len(run.started()) == 2 and [x["job_id"] for x in done] == [a]
    j.poll()
    assert len(done) == 1  # on_done once per completion
    assert (lay.jobs / a / "job.json").stat().st_mode & 0o777 == 0o600


def test_on_done_survives_restart(lay):
    run, done = FakeRun(), []
    a = jid()
    Jobs(lay, run=run).submit(a, "backup", {"mode": "light"})
    finish(lay, a)
    j = Jobs(lay, run=run, on_done=done.append)
    j.poll()
    j.poll()
    assert [x["job_id"] for x in done] == [a]
    Jobs(lay, run=run, on_done=done.append).poll()
    assert len(done) == 1


def test_submit_idempotent(lay):
    run = FakeRun()
    j = Jobs(lay, run=run)
    a = jid()
    first = j.submit(a, "backup", {"mode": "light"})
    assert j.submit(a, "backup", {"mode": "full"}) == first
    assert len(run.started()) == 1
    assert Jobs(lay, run=run).submit(a, "backup", {"mode": "light"})["job_id"] == a
    assert len(run.started()) == 1


def test_cancel_queued_and_running(lay):
    run = FakeRun()
    j = Jobs(lay, run=run)
    a, b = jid(), jid()
    j.submit(a, "backup", {"mode": "light"})
    j.submit(b, "backup", {"mode": "light"})
    assert j.cancel(b)["state"] == "cancelled"
    assert j.cancel(a)["state"] == "cancelled"
    assert ["systemctl", "--user", "stop", f"voxcut-job-{a}.service"] in run.calls
    j.poll()
    assert len(run.started()) == 1  # the cancelled queued job never starts
    run.active = "inactive"
    assert {v["state"] for v in Jobs(lay, run=run).list()} == {"cancelled"}


def test_dead_unit_without_status_is_failed(lay):
    run = FakeRun()
    a = jid()
    Jobs(lay, run=run).submit(a, "backup", {"mode": "light"})
    (lay.jobs / a / "status.json").write_text(json.dumps({"state": "running", "stage": "x",
                                                          "error": None}))
    run.active = "failed"
    view = Jobs(lay, run=run).list()[0]
    assert view["state"] == "failed" and view["error"] == {"code": "job_dead", "params": {}}
    run.active = "activating"
    b = jid()
    Jobs(lay, run=run).submit(b, "backup", {"mode": "light"})
    assert {v["job_id"]: v["state"] for v in Jobs(lay, run=run).list()}[b] == "running"


@pytest.mark.parametrize("kind, params", [
    ("shell", {}),
    ("import", {"source": "../data", "chain": 1, "files": ["a.wav"], "start": None}),
    ("import", {"source": "", "chain": 1, "files": ["a.wav"], "start": None}),  # inbox/ holds no WAV
    ("import", {"source": "s1", "chain": 0, "files": ["a.wav"], "start": None}),
    ("import", {"source": "s1", "chain": True, "files": ["a.wav"], "start": None}),
    ("import", {"source": "s1", "chain": 1, "files": ["a.wav"], "start": "вчера"}),
    ("import", {"source": "s1", "chain": 1, "start": None}),  # review #1: files required
    ("import", {"source": "s1", "chain": 1, "files": [], "start": None}),
    ("import", {"source": "s1", "chain": 1, "files": "a.wav", "start": None}),
    ("import", {"source": "s1", "chain": 1, "files": [1], "start": None}),
    ("import", {"source": "s1", "chain": 1, "files": ["a.wav"], "start": None, "cmd": "rm"}),
    ("process", {"session_id": SID}),
    ("process", {"session_id": "../" + SID}),
    ("backup", {"mode": "all"}),
    ("backup", {}),
    ("restore", {"name": "../backups/voxcut-backup_x_full.tar"}),
    ("restore", {"name": "voxcut-backup_x_full.tar"}),
])
def test_validate_rejects(lay, kind, params):
    (lay.inbox / "s1").mkdir()
    with pytest.raises(ValueError) as e:
        validate(kind, params, lay)
    assert e.value.code.startswith(("job_", "session_bad_start"))


def test_validate_accepts(lay):
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "s1" / "a.wav").write_bytes(b"")  # D50: a source folder holds WAV files
    (lay.inbox / "voxcut-backup_x_full.tar").write_bytes(b"")
    assert validate("import", {"source": "s1", "chain": 2, "files": ["a.wav"],
                               "start": "2026-08-11 21:30"}, lay) == {
        "source": "s1", "chain": 2, "files": ["a.wav"], "start": "2026-08-11 21:30"}
    assert validate("restore", {"name": "voxcut-backup_x_full.tar"}, lay)
    assert validate("backup", {"mode": "full"}, lay) == {"mode": "full"}


def test_submit_rejects_bad_job_id(lay):
    with pytest.raises(jobs.JobParamsError) as e:
        Jobs(lay, run=FakeRun()).submit("../x", "backup", {"mode": "light"})
    assert e.value.code == "job_bad_id"


def test_main_runs_backup_and_writes_status(lay):
    a = jid()
    Jobs(lay, run=FakeRun()).submit(a, "backup", {"mode": "light"})
    assert jobs.main([str(lay.jobs / a)]) == 0
    st = json.loads((lay.jobs / a / "status.json").read_text())
    assert st["state"] == "done" and st["result"].endswith("_light.tar")
    assert (lay.backups / st["result"]).is_file()


def test_main_failure_recorded(lay):
    a = jid()
    Jobs(lay, run=FakeRun()).submit(a, "backup", {"mode": "light"})
    job = json.loads((lay.jobs / a / "job.json").read_text())
    job["kind"], job["params"] = "process", {"session_id": SID}
    (lay.jobs / a / "job.json").write_text(json.dumps(job))
    assert jobs.main([str(lay.jobs / a)]) == 1
    st = json.loads((lay.jobs / a / "status.json").read_text())
    assert st["state"] == "failed" and st["error"]["code"] == "process_no_session"


@pytest.mark.slow
@pytest.mark.host
def test_real_unit_no_network(lay):
    from voxcut.ui.envcheck import PROBE_BLOCKED, run_probe

    ensure_slice(subprocess.run)
    if run_probe(lay.root, False) != 0:
        pytest.skip("сети нет и без PrivateNetwork: проба ничего не докажет")
    assert run_probe(lay.root, True) == PROBE_BLOCKED  # ENETUNREACH, not any failure (D63)


def test_server_side_modules_do_not_import_torch():
    code = "import sys, voxcut.jobs, voxcut.library, voxcut.journal; print('torch' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_on_done_error_does_not_block_queue(lay):
    run = FakeRun()

    def boom(job):
        raise ValueError("нет journal.jsonl")

    j = Jobs(lay, run=run, on_done=boom)
    a, b = jid(), jid()
    j.submit(a, "backup", {"mode": "light"})
    j.submit(b, "backup", {"mode": "light"})
    finish(lay, a)
    j.poll()
    views = {v["job_id"]: v for v in j.list()}
    assert views[b]["state"] == "running"
    assert views[a]["state"] == "failed" and views[a]["error"] == {
        "code": "job_after_done_failed", "params": {"cause": {
            "code": "job_unexpected", "params": {"detail": "ValueError: нет journal.jsonl"}}}}
    Jobs(lay, run=run, on_done=boom).poll()  # not retried forever after a restart


def test_view_has_result(lay):
    a = jid()
    Jobs(lay, run=FakeRun()).submit(a, "backup", {"mode": "light"})
    assert jobs.main([str(lay.jobs / a)]) == 0
    assert Jobs(lay, run=FakeRun(active="inactive")).list()[0]["result"].endswith("_light.tar")


def test_cancel_after_done_keeps_done(lay):
    run, done = FakeRun(), []
    j = Jobs(lay, run=run, on_done=done.append)
    a = jid()
    j.submit(a, "backup", {"mode": "light"})
    finish(lay, a)  # the unit finished right before «отмена»
    assert j.cancel(a)["state"] == "done"
    j.poll()
    assert [x["job_id"] for x in done] == [a]


def test_sigterm_runs_cleanup(tmp_path):
    """systemctl stop sends SIGTERM: the job's cleanup (e.g. *.tar.tmp removal) must run."""
    flag = tmp_path / "cleaned"
    code = (f"import os, signal, time\nfrom voxcut import jobs\njobs._exit_on_sigterm()\n"
            f"try:\n    os.kill(os.getpid(), signal.SIGTERM)\n    time.sleep(5)\n"
            f"finally:\n    open({str(flag)!r}, 'w').close()\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, check=False)
    assert r.returncode == 143 and flag.exists()


@pytest.mark.parametrize("sid", [["x"], {"a": 1}, 5])
def test_validate_session_id_type(lay, sid):
    with pytest.raises(ValueError):
        validate("process", {"session_id": sid}, lay)


def test_validate_process_needs_can_process(lay):
    d = lay.sessions / SID
    d.mkdir()
    (d / "import.json").write_text("{}")  # light: no originals, no WAV
    with pytest.raises(ValueError, match="cannot be processed") as e:
        validate("process", {"session_id": SID}, lay)
    assert e.value.code == "job_cannot_process"


def test_legacy_string_error(lay):
    """Jobs from before D66 keep a Russian string in status.json: shown as is, not a crash."""
    a = jid()
    Jobs(lay, run=FakeRun()).submit(a, "backup", {"mode": "light"})
    (lay.jobs / a / "status.json").write_text(json.dumps(
        {"state": "failed", "stage": None, "error": "старый текст"}))
    view = Jobs(lay, run=FakeRun()).list()[0]
    assert view["error"] == {"code": "legacy_error", "params": {"detail": "старый текст"}}


def test_unit_uncoded_exception(lay, monkeypatch):
    a = jid()
    Jobs(lay, run=FakeRun()).submit(a, "backup", {"mode": "light"})
    job = json.loads((lay.jobs / a / "job.json").read_text())
    job["kind"], job["params"] = "process", {"session_id": SID}
    (lay.jobs / a / "job.json").write_text(json.dumps(job))

    def boom(*a, **kw):
        raise KeyError("n_samples")

    monkeypatch.setattr(jobs.library, "process_session", boom)
    assert jobs.main([str(lay.jobs / a)]) == 1
    st = json.loads((lay.jobs / a / "status.json").read_text())
    # user request 2026-10-23: show the exception text
    assert st["error"] == {"code": "job_unexpected", "params": {"detail": "KeyError: 'n_samples'"}}
    assert "KeyError: 'n_samples'" in (lay.jobs / a / "log.txt").read_text()


@pytest.mark.parametrize("exc, cause", [
    (JobError("restore_journal_broken", detail="x"),
     {"code": "restore_journal_broken", "params": {"detail": "x"}}),
    (KeyError("p"), {"code": "job_unexpected", "params": {"detail": "KeyError: 'p'"}}),
])
def test_after_done_cause(lay, exc, cause):
    def boom(job):
        raise exc

    j = Jobs(lay, run=FakeRun(), on_done=boom)
    a = jid()
    j.submit(a, "backup", {"mode": "light"})
    finish(lay, a)
    j.poll()
    assert j.list()[0]["error"] == {"code": "job_after_done_failed", "params": {"cause": cause}}


def as_job(lay, kind, params, state=None, run=None):
    """A job of any kind without building its inputs: submit a backup, then rewrite job.json."""
    a = jid()
    Jobs(lay, run=run or FakeRun()).submit(a, "backup", {"mode": "light"})
    job = json.loads((lay.jobs / a / "job.json").read_text())
    job["kind"], job["params"] = kind, params
    (lay.jobs / a / "job.json").write_text(json.dumps(job))
    if state:
        finish(lay, a, state)
    return a


@pytest.mark.parametrize("kind, params, subject", [
    ("process", {"session_id": SID}, {"session_id": SID}),
    ("import", {"source": "s1", "chain": 2, "files": ["REC0001.WAV"], "start": "2026-08-11 21:30"},
     {"source": "s1"}),
    ("backup", {"mode": "full"}, {"mode": "full"}),
    ("restore", {"name": "voxcut-backup_x_full.tar"}, {"name": "voxcut-backup_x_full.tar"}),
])
def test_view_subject_whitelist(lay, kind, params, subject):
    """D79: subject by a whitelist; files, chain, start never leave job.json."""
    a = as_job(lay, kind, params, "done")
    view = Jobs(lay, run=FakeRun()).list()[0]
    assert view["subject"] == subject
    dumped = json.dumps(view)
    for leak in ('"files"', '"chain"', '"start"', "REC0001"):
        assert leak not in dumped
    assert view["submitted"] == json.loads((lay.jobs / a / "job.json").read_text())["submitted"]


def test_view_old_job_without_submitted(lay):
    a = as_job(lay, "backup", {"mode": "light"}, "done")
    job = json.loads((lay.jobs / a / "job.json").read_text())
    del job["submitted"]
    (lay.jobs / a / "job.json").write_text(json.dumps(job))
    assert Jobs(lay, run=FakeRun()).list()[0]["submitted"] is None


def imp(source, files):
    return ("import", {"source": source, "chain": 1, "files": files, "start": None})


S2 = "2026-08-12_21-30_1h.0badc0df"
A, B = "voxcut-backup_a_full.tar", "voxcut-backup_b_full.tar"


@pytest.mark.parametrize("seq, expected", [
    ([("process", {"session_id": SID}, "failed"), ("process", {"session_id": SID}, "done")],
     "resolved"),
    ([("process", {"session_id": SID}, "failed"), ("process", {"session_id": S2}, "done")], "open"),
    ([("process", {"session_id": SID}, "failed"), ("process", {"session_id": SID}, "cancelled")],
     "open"),
    ([(*imp("s1", ["a.wav", "b.wav"]), "failed"), (*imp("s1", ["a.wav"]), "done"),
      (*imp("s1", ["b.wav"]), "done")], "resolved"),
    ([(*imp("s1", ["a.wav", "b.wav"]), "failed"), (*imp("s1", ["a.wav", "c.wav"]), "done")], "open"),
    ([(*imp("s1", ["a.wav"]), "failed"), (*imp("s2", ["a.wav"]), "done")], "open"),  # D87
    ([("backup", {"mode": "light"}, "failed"), ("backup", {"mode": "full"}, "done")], "resolved"),
    ([("backup", {"mode": "light"}, "failed"), ("backup", {"mode": "light"}, "done")], "resolved"),
    ([("backup", {"mode": "full"}, "failed"), ("backup", {"mode": "light"}, "done")], "open"),
    ([("backup", {"mode": "full"}, "failed"), ("backup", {"mode": "full"}, "done")], "resolved"),
    ([("backup", {"mode": "full"}, "failed"), ("process", {"session_id": SID}, "done")], "open"),
    ([("restore", {"name": A}, "failed"), ("restore", {"name": A}, "done")], "resolved"),
    ([("restore", {"name": A}, "failed"), ("restore", {"name": B}, "done")], "open"),
    ([(*imp("gone", ["a.wav"]), "failed")], "moot"),
    ([("restore", {"name": "voxcut-backup_gone_full.tar"}, "failed")], "moot"),
    ([(*imp("gone", ["a.wav"]), "failed"), (*imp("gone", ["a.wav"]), "done")], "resolved"),
])
def test_failure_state(lay, seq, expected):
    """D84, D87: resolved > moot > open, computed from the job log and inbox/."""
    a_session(lay)  # D96: a process failure of a missing session is moot
    for src in ("s1", "s2"):
        (lay.inbox / src).mkdir()
        (lay.inbox / src / "a.wav").write_bytes(b"")
    (lay.inbox / "s1" / "b.wav").write_bytes(b"")  # D89: a file still in its folder keeps it open
    for name in (A, B):
        (lay.inbox / name).write_bytes(b"")
    ids = [as_job(lay, kind, params, state) for kind, params, state in seq]
    views = {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}
    assert views[ids[0]]["failure"] == expected
    assert all(views[i]["failure"] is None for i, (*_, st) in zip(ids, seq) if st != "failed")


def test_failure_done_before_does_not_resolve(lay):
    (lay.inbox / A).write_bytes(b"")
    as_job(lay, "restore", {"name": A}, "done")
    a = as_job(lay, "restore", {"name": A}, "failed")
    assert {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[a]["failure"] == "open"


def test_failure_import_without_files(lay):
    """A job.json from before files were required: no crash, the failure stays open."""
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "s1" / "a.wav").write_bytes(b"")
    a = as_job(lay, "import", {"source": "s1", "chain": 1, "start": None}, "failed")
    as_job(lay, *imp("s1", ["a.wav"]), "done")
    assert {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[a]["failure"] == "open"


def test_list_keeps_open_failure_beyond_window(lay):
    """D84: an open failure never drops out of /api/state silently."""
    (lay.inbox / A).write_bytes(b"")
    a = as_job(lay, "restore", {"name": A}, "failed")
    for _ in range(LIST_MAX + 5):
        as_job(lay, "backup", {"mode": "light"}, "done")
    view = Jobs(lay, run=FakeRun()).list()
    assert len(view) == LIST_MAX + 1 and view[0]["job_id"] == a and view[0]["failure"] == "open"
    as_job(lay, "restore", {"name": A}, "done")
    assert a not in [v["job_id"] for v in Jobs(lay, run=FakeRun()).list()]


def test_retry_resubmits_same_params(lay):
    old = as_job(lay, "backup", {"mode": "full"}, "failed")
    new = jid()
    view = Jobs(lay, run=FakeRun()).retry(old, new)
    assert view["job_id"] == new and view["kind"] == "backup" and view["state"] == "running"
    assert json.loads((lay.jobs / new / "job.json").read_text())["params"] == {"mode": "full"}


@pytest.mark.parametrize("state, later", [
    ("done", None), ("cancelled", None),
    ("failed", ("backup", {"mode": "full"}, "done")),  # resolved
])
def test_retry_refusals(lay, state, later):
    old = as_job(lay, "backup", {"mode": "full"}, state)
    if later:
        as_job(lay, *later)
    with pytest.raises(jobs.JobParamsError) as e:
        Jobs(lay, run=FakeRun()).retry(old, jid())
    assert e.value.code == "job_retry_not_open"


def test_retry_refuses_moot(lay):
    old = as_job(lay, "restore", {"name": "voxcut-backup_gone_full.tar"}, "failed")
    with pytest.raises(jobs.JobParamsError) as e:
        Jobs(lay, run=FakeRun()).retry(old, jid())
    assert e.value.code == "job_retry_not_open"


def test_retry_unknown_job(lay):
    with pytest.raises(KeyError):
        Jobs(lay, run=FakeRun()).retry(jid(), jid())


def test_retry_idempotent_and_duplicate(lay):
    old = as_job(lay, "backup", {"mode": "full"}, "failed")
    j, new = Jobs(lay, run=FakeRun()), jid()
    assert j.retry(old, new) == j.retry(old, new)
    assert len(j.list()) == 2
    with pytest.raises(jobs.JobParamsError) as e:
        j.retry(old, jid())
    assert e.value.code == "job_retry_duplicate"


def test_retry_runs_validate(lay):
    d = lay.sessions / SID
    d.mkdir()
    (d / "import.json").write_text("{}")  # light: cannot be processed
    old = as_job(lay, "process", {"session_id": SID}, "failed")
    with pytest.raises(jobs.JobParamsError) as e:
        Jobs(lay, run=FakeRun()).retry(old, jid())
    assert e.value.code == "job_retry_not_open"  # D96: such a failure is moot, nothing to retry


@pytest.mark.parametrize("files, code", [
    (["a.wav", "gone.wav"], "job_files_gone"),
    (["../x.wav"], "job_bad_files"),
    (["a/b.wav"], "job_bad_files"),
    (["."], "job_bad_files"),
    ([".."], "job_bad_files"),
])
def test_validate_import_files_present(lay, files, code):
    """D82: a retry of an import refuses at once; names never probe outside the folder."""
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "s1" / "a.wav").write_bytes(b"")
    with pytest.raises(jobs.JobParamsError) as e:
        validate("import", {"source": "s1", "chain": 1, "files": files, "start": None}, lay)
    assert e.value.code == code


def test_failure_retried(lay):
    """D88: a later failure of the same job supersedes it; only the newest stays open."""
    old = as_job(lay, "backup", {"mode": "full"}, "failed")
    mid = as_job(lay, "backup", {"mode": "full"}, "failed")
    other = as_job(lay, "backup", {"mode": "light"}, "failed")
    new = as_job(lay, "backup", {"mode": "full"}, "failed")
    got = {v["job_id"]: v["failure"] for v in Jobs(lay, run=FakeRun()).list()}
    assert [got[old], got[mid], got[other], got[new]] == ["retried", "retried", "open", "open"]
    as_job(lay, "backup", {"mode": "full"}, "done")
    got = {v["job_id"]: v["failure"] for v in Jobs(lay, run=FakeRun()).list()}
    assert [got[old], got[new]] == ["resolved", "resolved"]


def session_import(lay, name, source, files, pending=False):
    d = lay.sessions / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ("import.pending.json" if pending else "import.json")).write_text(json.dumps(
        {"source": source, "files": [{"name": f, "sha256": "0" * 64} for f in files]}))


@pytest.mark.parametrize("setup, expected", [
    (lambda lay: session_import(lay, SID, "s1", ["a.wav", "b.wav"]), "resolved"),  # «Обработать» finished it
    (lambda lay: session_import(lay, SID, "s2", ["a.wav", "b.wav"]), "moot"),  # same names, other folder
    (lambda lay: session_import(lay, SID, "s1", ["a.wav", "b.wav"], pending=True), "open"),  # to finish
    (lambda lay: (lay.inbox / "s1" / "a.wav").write_bytes(b""), "moot"),  # b.wav taken away by hand
    (lambda lay: [(lay.inbox / "s1" / f).write_bytes(b"") for f in ("a.wav", "b.wav")], "open"),
])
def test_failure_import_files_where(lay, setup, expected):
    """D89: resolved once the files are in a session; moot once one is neither in inbox/ nor pending."""
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "s1" / "c.wav").write_bytes(b"")  # another chain keeps the folder listed
    a = as_job(lay, *imp("s1", ["a.wav", "b.wav"]), "failed")
    setup(lay)
    assert {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[a]["failure"] == expected


def test_failure_import_bad_session_json(lay):
    """One unreadable import.json must not take /api/state down."""
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "s1" / "a.wav").write_bytes(b"")
    a = as_job(lay, *imp("s1", ["a.wav"]), "failed")
    (lay.sessions / SID).mkdir(parents=True)
    (lay.sessions / SID / "import.json").write_text("{не json")
    assert {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[a]["failure"] == "open"


# --- delete a hidden session (D94) ---

def a_session(lay, sid=SID):
    d = lay.sessions / sid
    (d / "originals").mkdir(parents=True)
    (d / "import.json").write_text("{}")
    return sid


@pytest.mark.parametrize("sid, hidden, code", [
    (SID, lambda s: False, "job_session_not_hidden"),
    (SID, None, "job_session_not_hidden"),
    ("2026-01-01_00-00_1h00m.00000000", lambda s: True, "job_no_session"),
    ("../x", lambda s: True, "job_no_session"),
    (5, lambda s: True, "job_bad_session_id"),
])
def test_validate_delete(lay, sid, hidden, code):
    a_session(lay)
    with pytest.raises(ValueError) as e:
        validate("delete", {"session_id": sid}, lay, hidden)
    assert e.value.code == code
    assert validate("delete", {"session_id": SID}, lay, lambda s: s == SID) == {"session_id": SID}


def test_busy_session_refuses(lay):
    a_session(lay)
    j = Jobs(lay, run=FakeRun(), hidden=lambda s: True)
    j.submit(jid(), "process", {"session_id": SID})
    assert j.busy(SID)
    with pytest.raises(ValueError) as e:
        j.submit(jid(), "delete", {"session_id": SID})
    assert e.value.code == "job_session_busy"


def test_delete_job_forgets_process_jobs(lay):
    a_session(lay)
    mine = [as_job(lay, "process", {"session_id": SID}, state) for state in ("failed", "done")]
    other = as_job(lay, "process", {"session_id": S2}, "failed")
    j = Jobs(lay, run=FakeRun(), hidden=lambda s: True)
    d = jid()
    j.submit(d, "delete", {"session_id": SID})
    finish(lay, d)
    j.poll()
    ids = [v["job_id"] for v in j.list()]
    assert other in ids and d in ids and not set(mine) & set(ids)
    assert not any((lay.jobs / m).exists() for m in mine)
    assert [v["job_id"] for v in Jobs(lay, run=FakeRun()).list()] == ids  # also after a restart


def test_cancel_delete_refused(lay):
    a_session(lay)
    j = Jobs(lay, run=FakeRun(), hidden=lambda s: True)
    d = jid()
    j.submit(d, "delete", {"session_id": SID})
    with pytest.raises(ValueError) as e:
        j.cancel(d)
    assert e.value.code == "job_cancel_delete"
    assert j.list()[0]["state"] == "running"


# --- job-log minors (D96) ---

def test_order_by_submitted_after_restart(lay):
    a = as_job(lay, "backup", {"mode": "light"}, "done")
    b = as_job(lay, "backup", {"mode": "full"}, "done")
    for job_id, ts in ((a, "2026-10-25T10:00:00+03:00"), (b, "2026-10-25T10:00:01+03:00")):
        job = json.loads((lay.jobs / job_id / "job.json").read_text())
        (lay.jobs / job_id / "job.json").write_text(json.dumps(job | {"submitted": ts}))
    os.utime(lay.jobs / a, (2e9, 2e9))  # a hand touch or a copy of .run/ reorders mtimes
    assert [v["job_id"] for v in Jobs(lay, run=FakeRun()).list()] == [a, b]


def test_bad_params_job_does_not_break_list(lay):
    a = as_job(lay, "import", [1], "failed")
    view = {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[a]
    assert view["subject"] == {} and view["state"] == "failed"


def test_retry_inner_keyerror_is_not_no_job(lay, monkeypatch):
    old = as_job(lay, "backup", {"mode": "full"}, "failed")
    j = Jobs(lay, run=FakeRun())
    monkeypatch.setattr(j, "_failures", lambda: {}["x"])
    with pytest.raises(KeyError) as e:
        j.retry(old, jid())
    assert not isinstance(e.value, jobs.NoJob)
    with pytest.raises(jobs.NoJob):
        j.retry(jid(), jid())
    with pytest.raises(jobs.NoJob):
        j.cancel(jid())


@pytest.mark.parametrize("case", ["missing", "light"])
def test_failure_process_moot(lay, case):
    if case == "light":
        (lay.sessions / SID).mkdir()
        (lay.sessions / SID / "import.json").write_text("{}")  # no originals, no WAV
    a = as_job(lay, "process", {"session_id": SID}, "failed")
    assert Jobs(lay, run=FakeRun()).list()[0]["failure"] == "moot"
    assert a


def test_view_retrying(lay):
    old = as_job(lay, "backup", {"mode": "full"}, "failed")
    j = Jobs(lay, run=FakeRun())
    assert {v["job_id"]: v for v in j.list()}[old]["retrying"] is False
    new = jid()
    j.retry(old, new)
    assert {v["job_id"]: v for v in j.list()}[old]["retrying"] is True
    finish(lay, new, "failed")
    j.poll()
    assert {v["job_id"]: v for v in j.list()}[old]["retrying"] is False


def test_retry_delete_cut_after_rename(lay):
    """Layer-1 review: the session dir is already sessions/<sid>.tmp; a retry must finish it."""
    gone = lay.sessions / f"{SID}.tmp"
    gone.mkdir()
    (gone / "delete.pending.json").write_text(json.dumps({"files": {}, "start": None, "notes": 0,
                                                          "notes_text": ""}))
    old = as_job(lay, "delete", {"session_id": SID}, "failed")
    j = Jobs(lay, run=FakeRun(), hidden=lambda s: False)  # no session row, so no hidden flag either
    assert {v["job_id"]: v for v in j.list()}[old]["failure"] == "open"
    assert j.retry(old, jid())["state"] == "running"


@pytest.mark.parametrize("later, expected", [("done", "resolved"), (None, "moot")])
def test_failure_delete(lay, later, expected):
    a_session(lay, S3 := "2026-10-15_10-00_1h00m.0000aaaa")
    old = as_job(lay, "delete", {"session_id": SID}, "failed")
    as_job(lay, "delete", {"session_id": S3}, "done")  # another session's delete resolves nothing
    if later:
        as_job(lay, "delete", {"session_id": SID}, later)
    assert {v["job_id"]: v for v in Jobs(lay, run=FakeRun()).list()}[old]["failure"] == expected


def test_order_survives_offset_change(lay):
    a = as_job(lay, "backup", {"mode": "light"}, "done")
    b = as_job(lay, "backup", {"mode": "full"}, "done")
    for job_id, ts in ((a, "2026-10-25T02:30:00+03:00"), (b, "2026-10-25T02:10:00+02:00")):
        job = json.loads((lay.jobs / job_id / "job.json").read_text())  # a DST change in between
        (lay.jobs / job_id / "job.json").write_text(json.dumps(job | {"submitted": ts}))
    assert [v["job_id"] for v in Jobs(lay, run=FakeRun()).list()] == [a, b]


def test_failed_delete_moot_once_session_restored(lay):
    """Final review: the owner kept the session (Restore): its failed delete is no longer an alarm."""
    a_session(lay)
    old = as_job(lay, "delete", {"session_id": SID}, "failed")
    shown = Jobs(lay, run=FakeRun(), hidden=lambda s: False).list()
    assert {v["job_id"]: v for v in shown}[old]["failure"] == "moot"
    still = Jobs(lay, run=FakeRun(), hidden=lambda s: True).list()
    assert {v["job_id"]: v for v in still}[old]["failure"] == "open"


def test_retry_delete_cut_inside_erase(lay):
    (lay.sessions / f"{SID}.tmp" / "x").mkdir(parents=True)  # no pending file left
    old = as_job(lay, "delete", {"session_id": SID}, "failed")
    assert Jobs(lay, run=FakeRun(), hidden=lambda s: False).retry(old, jid())["state"] == "running"


def test_when_idle_runs_under_the_jobs_lock(lay):
    """Hide (D94) and a new job of the session cannot interleave."""
    a_session(lay)
    j = Jobs(lay, run=FakeRun(), hidden=lambda s: False)
    assert j.when_idle(SID, lambda: j._lock.locked()) is True
    j.submit(jid(), "process", {"session_id": SID})
    with pytest.raises(JobParamsError) as e:
        j.when_idle(SID, lambda: pytest.fail("ran on a busy session"))
    assert e.value.code == "job_session_busy"
