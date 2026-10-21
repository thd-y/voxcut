import json
import subprocess
import sys
import uuid

import pytest

from voxcut import jobs
from voxcut.jobs import JobError, Jobs, ensure_slice, unit_argv, validate
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
    with pytest.raises(JobError, match="лимит"):
        ensure_slice(run)
    j = Jobs(lay, run=run)
    view = j.submit(jid(), "backup", {"mode": "light"})
    assert view["state"] == "failed" and "лимит" in view["error"]
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
    states = {v["job_id"]: v["state"] for v in j.poll()}
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
    assert Jobs(lay, run=run, on_done=done.append).poll() and len(done) == 1


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
    assert view["state"] == "failed" and "OOM" in view["error"]
    run.active = "activating"
    b = jid()
    Jobs(lay, run=run).submit(b, "backup", {"mode": "light"})
    assert {v["job_id"]: v["state"] for v in Jobs(lay, run=run).list()}[b] == "running"


@pytest.mark.parametrize("kind, params", [
    ("shell", {}),
    ("import", {"source": "../data", "chain": 1, "start": None}),
    ("import", {"source": "", "chain": 1, "start": None}),  # inbox/ holds no WAV
    ("import", {"source": "s1", "chain": 0, "start": None}),
    ("import", {"source": "s1", "chain": True, "start": None}),
    ("import", {"source": "s1", "chain": 1, "start": "вчера"}),
    ("import", {"source": "s1", "chain": 1, "start": None, "cmd": "rm"}),
    ("process", {"session_id": SID}),
    ("process", {"session_id": "../" + SID}),
    ("backup", {"mode": "all"}),
    ("backup", {}),
    ("restore", {"name": "../backups/voxcut-backup_x_full.tar"}),
    ("restore", {"name": "voxcut-backup_x_full.tar"}),
])
def test_validate_rejects(lay, kind, params):
    (lay.inbox / "s1").mkdir()
    with pytest.raises(ValueError):
        validate(kind, params, lay)


def test_validate_accepts(lay):
    (lay.inbox / "s1").mkdir()
    (lay.inbox / "voxcut-backup_x_full.tar").write_bytes(b"")
    assert validate("import", {"source": "s1", "chain": 2, "start": "2026-08-11 21:30"}, lay) == {
        "source": "s1", "chain": 2, "start": "2026-08-11 21:30"}
    assert validate("restore", {"name": "voxcut-backup_x_full.tar"}, lay)
    assert validate("backup", {"mode": "full"}, lay) == {"mode": "full"}


def test_submit_rejects_bad_job_id(lay):
    with pytest.raises(ValueError):
        Jobs(lay, run=FakeRun()).submit("../x", "backup", {"mode": "light"})


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
    assert st["state"] == "failed" and "нет сессии" in st["error"]


@pytest.mark.slow
def test_real_unit_no_network(lay):
    ensure_slice(subprocess.run)
    probe = [sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"]

    def run_unit(argv):
        return subprocess.run([argv[0], "--wait", "--pipe", *argv[1:]], capture_output=True,
                              text=True, timeout=60, check=False).returncode

    argv = unit_argv(f"voxcut-probe-{jid()[:8]}", probe, lay.root)
    i = argv.index("PrivateNetwork=yes")
    control = unit_argv(f"voxcut-probe-{jid()[:8]}", probe, lay.root)
    del control[i - 1:i + 1]  # the same unit with the network left on
    if run_unit(control) != 0:
        pytest.skip("сети нет и без PrivateNetwork: проба ничего не докажет")
    assert run_unit(argv) != 0


def test_server_side_modules_do_not_import_torch():
    code = "import sys, voxcut.jobs, voxcut.library, voxcut.journal; print('torch' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
