import json
import subprocess
import sys
import uuid

import pytest

from voxcut import jobs
from voxcut.jobs import LIST_MAX, JobError, Jobs, ensure_slice, unit_argv, validate
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
    with pytest.raises(JobError, match="лимит") as e:
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
        "code": "job_after_done_failed", "params": {"cause": {"code": "internal_error",
                                                             "params": {}}}}
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
    with pytest.raises(ValueError, match="обработ") as e:
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
    assert st["error"] == {"code": "internal_error", "params": {}}
    assert "KeyError: 'n_samples'" in (lay.jobs / a / "log.txt").read_text()


@pytest.mark.parametrize("exc, cause", [
    (JobError("restore_journal_broken", detail="x"),
     {"code": "restore_journal_broken", "params": {"detail": "x"}}),
    (KeyError("p"), {"code": "internal_error", "params": {}}),
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
