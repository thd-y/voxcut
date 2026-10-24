"""Heavy steps as systemd user units in voxcut.slice (U9). The server never imports torch."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from voxcut import library
from voxcut.errors import Coded, descriptor
from voxcut.intervals import parse_start

KINDS = ("import", "process", "backup", "restore")
HEAVY = KINDS  # all heavy, one at a time
SLICE = "voxcut.slice"
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
DEAD = {"code": "job_dead", "params": {}}
FINAL = ("done", "failed", "cancelled")


class JobError(Coded, RuntimeError):
    pass


class JobParamsError(Coded, ValueError):
    """Every refusal of validate/submit: a 400 with its code, not internal_error."""


SUBJECT = {"process": ("session_id",), "import": ("source",), "backup": ("mode",),
           "restore": ("name",)}  # D79: whitelist, nothing else leaves job.json
LIST_MAX = 20  # (агент) jobs shown in /api/state; older finished ones stay on disk


def _job_error(exc: BaseException) -> dict:
    """An uncoded failure inside a job shows its type and text (user, 2026-10-23); HTTP 500 does not."""
    if isinstance(exc, Coded):
        return descriptor(exc)
    return {"code": "job_unexpected", "params": {"detail": f"{type(exc).__name__}: {exc}"}}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate(kind: str, params: dict, lay: library.Layout) -> dict:
    """Narrow contract (U9): no paths, commands or unit properties from the browser."""
    keys = {"import": {"source", "chain", "files", "start"}, "process": {"session_id"},
            "backup": {"mode"}, "restore": {"name"}}
    if not isinstance(kind, str) or kind not in keys:
        raise JobParamsError("job_unknown_kind", kind=repr(kind))
    if not isinstance(params, dict) or set(params) != keys[kind]:
        raise JobParamsError("job_bad_params", kind=kind, keys=str(sorted(keys[kind])))
    p = dict(params)
    if kind == "import":
        if p["source"] not in library._source_names(lay):
            raise JobParamsError("job_no_source")
        if type(p["chain"]) is not int or p["chain"] < 1:
            raise JobParamsError("job_bad_chain")
        if (not isinstance(p["files"], list) or not p["files"]
                or not all(isinstance(f, str) and f for f in p["files"])):
            raise JobParamsError("job_bad_files")
        if not all(f == Path(f).name and f not in (".", "..") for f in p["files"]):
            raise JobParamsError("job_bad_files")  # a name never probes outside the folder
        if not all((lay.inbox / p["source"] / f).is_file() for f in p["files"]):
            raise JobParamsError("job_files_gone")  # D82: a retry refuses at once
        if p["start"] is not None:
            if not isinstance(p["start"], str):
                raise JobParamsError("job_bad_start")
            parse_start(p["start"])  # SessionError is a ValueError
    elif kind == "process":
        if not isinstance(p["session_id"], str):
            raise JobParamsError("job_bad_session_id")
        row = library.session_row(lay, p["session_id"])
        if row is None:
            raise JobParamsError("job_no_session")
        if not row["can_process"]:
            raise JobParamsError("job_cannot_process")
    elif kind == "backup":
        if p["mode"] not in ("full", "light"):
            raise JobParamsError("job_bad_mode")
    elif p["name"] not in library._backup_names(lay):
        raise JobParamsError("job_no_archive")
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
        raise JobError("job_slice_limit", slice=SLICE, detail=repr(out.strip()))


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
                raise JobError("job_unit_failed", detail=r.stderr.strip())
        except JobError as exc:
            self._set_status(job_id, "failed", error=descriptor(exc))
            rec["state"] = "failed"
        except OSError as exc:
            self._set_status(job_id, "failed",
                             error={"code": "job_start_failed", "params": {"detail": str(exc)}})
            rec["state"] = "failed"

    def _step(self) -> None:
        self._refresh()
        for job_id, rec in self._jobs.items():
            marker = self.lay.jobs / job_id / "on_done"
            if rec["state"] == "done" and self.on_done and not marker.exists():
                try:
                    self.on_done(self._view(job_id))
                except Exception as exc:  # noqa: BLE001 - one bad hook must not stall the queue
                    self._set_status(job_id, "failed", error={
                        "code": "job_after_done_failed", "params": {"cause": _job_error(exc)}})
                    rec["state"] = "failed"  # the badge flags it; the job's work is only partly in
                finally:
                    marker.touch()
        if self._queue and not any(r["state"] == "running" for r in self._jobs.values()):
            self._start(self._queue.pop(0))

    def submit(self, job_id: str, kind: str, params: dict) -> dict:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise JobParamsError("job_bad_id")
        with self._lock:
            return self._add(job_id, kind, params)

    def _add(self, job_id: str, kind: str, params: dict) -> dict:
        if job_id not in self._jobs:
            job = {"job_id": job_id, "kind": kind, "params": validate(kind, params, self.lay),
                   "submitted": _now()}
            self._jobs[job_id] = {"job": job, "state": "queued"}
            self._queue.append(job_id)
            self._step()
        return self._view(job_id)

    def retry(self, job_id: str, new_id: str) -> dict:
        """D82: the same kind and params as a failed job whose failure is open; none from the browser."""
        if not isinstance(new_id, str) or not JOB_ID_RE.fullmatch(new_id):
            raise JobParamsError("job_bad_id")
        with self._lock:
            if new_id in self._jobs:
                return self._view(new_id)
            rec = self._jobs[job_id]  # KeyError: no such job
            if rec["state"] != "failed" or self._failures().get(job_id) != "open":
                raise JobParamsError("job_retry_not_open")
            kind, params = rec["job"]["kind"], rec["job"]["params"]
            if any(r["state"] in ("queued", "running") and r["job"]["kind"] == kind
                   and r["job"]["params"] == params for r in self._jobs.values()):
                raise JobParamsError("job_retry_duplicate")
            return self._add(new_id, kind, params)

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
                st = _read(self.lay.jobs / job_id / "status.json") or {}
                if st.get("state") in FINAL:  # it finished before the stop: keep its result
                    rec["state"] = st["state"]
                else:
                    self._set_status(job_id, "cancelled")
                    rec["state"] = "cancelled"
            return self._view(job_id)

    def poll(self) -> None:
        with self._lock:
            self._step()

    def list(self) -> list[dict]:
        """Every queued or running job, every open failure (D84) plus the newest finished ones;
        .run/jobs/ itself only grows."""
        with self._lock:
            ids, failures = list(self._jobs), self._failures()
            return [self._view(j, failures) for n, j in enumerate(ids) if n >= len(ids) - LIST_MAX
                    or self._jobs[j]["state"] in ("queued", "running") or failures.get(j) == "open"]

    def _failures(self) -> dict[str, str]:
        """D84, D87, D88: a failure is resolved by a later done job reaching its goal, retried when
        the same job failed again later, moot when the goal is gone from inbox/, otherwise open.
        "Later" is the order of self._jobs (one job at a time)."""
        out, done, failed = {}, [], []  # (kind, params) of done / failed jobs after the one judged
        sources = backups = None
        for job_id in reversed(self._jobs):
            rec = self._jobs[job_id]
            kind, p = rec["job"]["kind"], rec["job"].get("params") or {}
            if rec["state"] == "done":
                done.append((kind, p))
            if rec["state"] != "failed":
                continue
            failed_later, failed = (kind, p) in failed, failed + [(kind, p)]
            later = [q for k, q in done if k == kind]
            if kind == "process":
                resolved = any(q.get("session_id") == p.get("session_id") for q in later)
            elif kind == "import":
                files = p.get("files")
                got = {f for q in later if q.get("source") == p.get("source")
                       for f in q.get("files") or ()}
                resolved = bool(files) and set(files) <= got
            elif kind == "backup":
                resolved = any(q.get("mode") in (p.get("mode"), "full") for q in later)
            else:
                resolved = any(q.get("name") == p.get("name") for q in later)
            if resolved or failed_later:  # D88: a later identical failure supersedes this one
                out[job_id] = "resolved" if resolved else "retried"
                continue
            if kind == "import":
                sources = library._source_names(self.lay) if sources is None else sources
                moot = p.get("source") not in sources
            elif kind == "restore":
                backups = library._backup_names(self.lay) if backups is None else backups
                moot = p.get("name") not in backups
            else:
                moot = False
            out[job_id] = "moot" if moot else "open"
        return out

    def _view(self, job_id: str, failures: dict[str, str] | None = None) -> dict:
        rec, d = self._jobs[job_id], self.lay.jobs / job_id
        if failures is None:
            failures = self._failures() if rec["state"] == "failed" else {}
        st = _read(d / "status.json") or {}
        try:
            with open(d / "log.txt", errors="replace") as f:
                tail = [line.rstrip("\n") for line in deque(f, 20)]
        except OSError:
            tail = []
        error = st.get("error")
        if isinstance(error, str):  # status.json from before D66
            error = {"code": "legacy_error", "params": {"detail": error}}
        job = rec["job"]
        params = job.get("params") or {}
        return {"job_id": job_id, "kind": job["kind"], "state": rec["state"],
                "subject": {k: params[k] for k in SUBJECT.get(job["kind"], ()) if k in params},
                "submitted": job.get("submitted"), "failure": failures.get(job_id),
                "stage": st.get("stage"), "error": error,
                "started": st.get("started"), "finished": st.get("finished"),
                "result": st.get("result"), "log_tail": tail}


def _exit_on_sigterm() -> None:
    """systemctl stop sends SIGTERM; exiting via SystemExit runs cleanup (e.g. *.tar.tmp)."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def main(argv: list[str]) -> int:
    """python -m voxcut.jobs <job_dir>: runs inside the systemd unit."""
    _exit_on_sigterm()
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
                result = library.import_originals(lay, p["source"], p["chain"], p["start"], log,
                                                 files=p["files"])
            elif job["kind"] == "process":
                result = str(library.process_session(lay, p["session_id"], stage, log)
                             .relative_to(lay.root))
            elif job["kind"] == "backup":
                result = library.make_backup(lay, p["mode"], datetime.now())
            elif job["kind"] == "restore":
                result = library.restore_backup(lay, p["name"], d / "journal.jsonl")
            else:
                raise JobError("job_unit_unknown_kind", kind=repr(job["kind"]))
        except Exception as exc:  # noqa: BLE001 - every failure becomes a visible status
            log(traceback.format_exc())
            _write(d / "status.json", status | {"state": "failed", "error": _job_error(exc),
                                                "finished": _now()})
            return 1
    _write(d / "status.json", status | {"state": "done", "result": result, "finished": _now()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
