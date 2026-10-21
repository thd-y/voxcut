"""Heavy steps as systemd user units in voxcut.slice (U9). The server never imports torch."""

import json
import os
import re
import subprocess
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from voxcut import library
from voxcut.intervals import parse_start

KINDS = ("import", "process", "backup", "restore")
HEAVY = KINDS  # all heavy, one at a time
SLICE = "voxcut.slice"
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
DEAD = "процесс задачи завершился без итога (возможно, OOM)"
FINAL = ("done", "failed", "cancelled")


class JobError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate(kind: str, params: dict, lay: library.Layout) -> dict:
    """Narrow contract (U9): no paths, commands or unit properties from the browser."""
    keys = {"import": {"source", "chain", "start"}, "process": {"session_id"},
            "backup": {"mode"}, "restore": {"name"}}
    if kind not in keys:
        raise ValueError(f"неизвестный вид задачи: {kind!r}")
    if not isinstance(params, dict) or set(params) != keys[kind]:
        raise ValueError(f"параметры {kind}: нужны ровно {sorted(keys[kind])}")
    p = dict(params)
    if kind == "import":
        if p["source"] not in library._source_names(lay):
            raise ValueError("нет такого источника во входящих")
        if type(p["chain"]) is not int or p["chain"] < 1:
            raise ValueError("chain: целое >= 1")
        if p["start"] is not None:
            if not isinstance(p["start"], str):
                raise ValueError("start: строка или null")
            parse_start(p["start"])  # SessionError is a ValueError
    elif kind == "process":
        if p["session_id"] not in {r["session_id"] for r in library.list_sessions(lay)}:
            raise ValueError("нет такой сессии")
    elif kind == "backup":
        if p["mode"] not in ("full", "light"):
            raise ValueError("mode: full или light")
    elif p["name"] not in library._backup_names(lay):
        raise ValueError("нет такого архива во входящих")
    return p


def unit_argv(unit: str, cmd: list[str], root: Path) -> list[str]:
    return [
        "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", f"--slice={SLICE}",
        "-p", "MemoryMax=5G", "-p", "MemorySwapMax=0", "-p", "PrivateNetwork=yes",
        "-p", "UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy",
        "--setenv=HF_HUB_OFFLINE=1", "--setenv=TRANSFORMERS_OFFLINE=1",
        f"--working-directory={root}", "--", *cmd,
    ]


def ensure_slice(run=subprocess.run) -> None:
    """The budget is on the slice, not the unit: two 5G units must not share 7 GB RAM (U9)."""
    run(["systemctl", "--user", "set-property", "--runtime", SLICE, "MemoryMax=5G",
         "MemorySwapMax=0"], capture_output=True, text=True)
    out = run(["systemctl", "--user", "show", "-p", "MemoryMax", "-p", "MemorySwapMax", SLICE],
              capture_output=True, text=True).stdout
    got = dict(line.split("=", 1) for line in out.split() if "=" in line)
    if got != {"MemoryMax": "5368709120", "MemorySwapMax": "0"}:
        raise JobError(f"лимит памяти на {SLICE} не применился ({out.strip()!r}); "
                       "задача не запущена")


def _write(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Jobs:
    def __init__(self, lay: library.Layout, run=subprocess.run, on_done=None):
        self.lay, self.run, self.on_done = lay, run, on_done
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}  # job_id -> {"job": job.json, "state", ...}
        self._queue: list[str] = []  # in memory only: lost on restart (see plan)
        for d in sorted(lay.jobs.iterdir(), key=lambda d: d.stat().st_mtime):
            job = _read(d / "job.json")
            if job is not None and JOB_ID_RE.fullmatch(d.name):
                self._jobs[d.name] = {"job": job, "state": "running"}
        with self._lock:
            self._refresh()

    def _unit(self, job_id: str) -> str:
        return f"voxcut-job-{job_id}.service"

    def _refresh(self) -> None:
        for job_id, rec in self._jobs.items():
            if rec["state"] != "running":
                continue
            st = _read(self.lay.jobs / job_id / "status.json") or {}
            if st.get("state") in FINAL:
                rec["state"] = st["state"]
                continue
            active = self.run(["systemctl", "--user", "is-active", self._unit(job_id)],
                              capture_output=True, text=True).stdout.strip()
            if active not in ("active", "activating"):
                st = _read(self.lay.jobs / job_id / "status.json") or {}  # it may just have ended
                if st.get("state") in FINAL:
                    rec["state"] = st["state"]
                else:
                    self._set_status(job_id, "failed", error=DEAD)
                    rec["state"] = "failed"

    def _set_status(self, job_id: str, state: str, **kw) -> None:
        path = self.lay.jobs / job_id / "status.json"
        st = (_read(path) or {"stage": None, "error": None}) | {"state": state} | kw
        st.setdefault("finished", _now())
        _write(path, st)

    def _start(self, job_id: str) -> None:
        rec = self._jobs[job_id]
        d = self.lay.jobs / job_id
        d.mkdir(mode=0o700)
        _write(d / "job.json", rec["job"])
        _write(d / "status.json", {"state": "running", "stage": None, "error": None,
                                   "started": _now()})
        rec["state"] = "running"
        try:
            ensure_slice(self.run)
            cmd = [sys.executable, "-m", "voxcut.jobs", str(d)]
            r = self.run(unit_argv(self._unit(job_id).removesuffix(".service"), cmd,
                                   self.lay.root), capture_output=True, text=True)
            if r.returncode != 0:
                raise JobError(f"systemd-run: {r.stderr.strip()}")
        except (JobError, OSError) as exc:
            self._set_status(job_id, "failed", error=str(exc))
            rec["state"] = "failed"

    def _step(self) -> None:
        self._refresh()
        for job_id, rec in self._jobs.items():
            marker = self.lay.jobs / job_id / "on_done"
            if rec["state"] == "done" and self.on_done and not marker.exists():
                self.on_done(self._view(job_id))
                marker.touch()
        if self._queue and not any(r["state"] == "running" for r in self._jobs.values()):
            self._start(self._queue.pop(0))

    def submit(self, job_id: str, kind: str, params: dict) -> dict:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("job_id: нужны 32 hex")
        with self._lock:
            if job_id not in self._jobs:
                job = {"job_id": job_id, "kind": kind, "params": validate(kind, params, self.lay),
                       "submitted": _now()}
                self._jobs[job_id] = {"job": job, "state": "queued"}
                self._queue.append(job_id)
                self._step()
            return self._view(job_id)

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(job_id)
            if rec["state"] == "queued":
                self._queue.remove(job_id)
                rec["state"] = "cancelled"
            elif rec["state"] == "running":
                self.run(["systemctl", "--user", "stop", self._unit(job_id)],
                         capture_output=True, text=True)
                self._set_status(job_id, "cancelled")
                rec["state"] = "cancelled"
            return self._view(job_id)

    def poll(self) -> list[dict]:
        with self._lock:
            self._step()
            return [self._view(j) for j in self._jobs]

    def list(self) -> list[dict]:
        with self._lock:
            return [self._view(j) for j in self._jobs]

    def _view(self, job_id: str) -> dict:
        rec, d = self._jobs[job_id], self.lay.jobs / job_id
        st = _read(d / "status.json") or {}
        try:
            with open(d / "log.txt", errors="replace") as f:
                tail = [line.rstrip("\n") for line in deque(f, 20)]
        except OSError:
            tail = []
        return {"job_id": job_id, "kind": rec["job"]["kind"], "state": rec["state"],
                "stage": st.get("stage"), "error": st.get("error"),
                "started": st.get("started"), "finished": st.get("finished"),
                "log_tail": tail}


def main(argv: list[str]) -> int:
    """python -m voxcut.jobs <job_dir>: runs inside the systemd unit."""
    d = Path(argv[0]).resolve()
    lay = library.Layout(d.parent.parent.parent)  # <root>/.run/jobs/<job_id>
    job = json.loads((d / "job.json").read_text())
    p = job["params"]
    status = {"state": "running", "stage": None, "error": None, "started": _now()}
    _write(d / "status.json", status)
    with open(d / "log.txt", "a", buffering=1) as logf:
        def log(msg: str) -> None:
            logf.write(f"{msg}\n")

        def stage(name: str) -> None:
            status["stage"] = name
            _write(d / "status.json", status)
            log(f"== {name}")

        try:
            if job["kind"] == "import":
                result = library.import_originals(lay, p["source"], p["chain"], p["start"], log)
            elif job["kind"] == "process":
                result = str(library.process_session(lay, p["session_id"], stage, log)
                             .relative_to(lay.root))
            elif job["kind"] == "backup":
                result = library.make_backup(lay, p["mode"], datetime.now())
            elif job["kind"] == "restore":
                result = library.restore_backup(lay, p["name"], d / "journal.jsonl")
            else:
                raise JobError(f"неизвестный вид задачи {job['kind']!r}")
        except Exception as exc:  # noqa: BLE001 - every failure becomes a visible status
            log(traceback.format_exc())
            _write(d / "status.json", status | {"state": "failed", "error": str(exc),
                                                "finished": _now()})
            return 1
    _write(d / "status.json", status | {"state": "done", "result": result, "finished": _now()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
