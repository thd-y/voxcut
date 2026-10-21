"""Product folders and operations on them (U3–U6). No torch: heavy steps run in jobs."""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from voxcut import audio
from voxcut.audio import RunError, SourceError, load_json, read_jsonl
from voxcut.cli import read_config, segment
from voxcut.intervals import SessionError

SESSION_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_\d+h\d{2}m\.[0-9a-f]{8}")


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def sessions(self) -> Path:
        return self.data / "sessions"

    @property
    def journal(self) -> Path:
        return self.data / "journal" / "journal.jsonl"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def run(self) -> Path:
        return self.root / ".run"

    @property
    def jobs(self) -> Path:
        return self.run / "jobs"

    def ensure(self) -> None:
        for d in (self.inbox, self.sessions, self.journal.parent, self.backups, self.jobs):
            d.mkdir(parents=True, exist_ok=True)
        self.run.chmod(0o700)


def _seg_dirs(sdir: Path, done: str) -> list[Path]:
    """seg-<cfg8> dirs of a session that contain `done` (manifest.jsonl or clips/manifest.jsonl)."""
    return [m.parent if done == "manifest.jsonl" else m.parent.parent
            for m in sdir.glob(f"*/seg-*/{done}")]


def session_runs(lay: Layout, sid: str) -> list[dict]:
    runs = []
    for seg in _seg_dirs(lay.sessions / sid, "clips/manifest.jsonl"):
        mtime = (seg / "clips" / "manifest.jsonl").stat().st_mtime
        rel = str(seg.relative_to(lay.sessions))
        runs.append((mtime, {
            "run_id": hashlib.sha256(rel.encode()).hexdigest()[:12],
            "name": load_json(seg / "run.json").get("meta", {}).get("name", seg.name),
            "created": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
            "seg_dir": seg,
        }))
    return [r for _, r in sorted(runs, key=lambda x: x[0], reverse=True)]


def _session_row(lay: Layout, sdir: Path) -> dict:
    sid = sdir.name
    sess = sdir / f"{sid}.session"
    meta = load_json(sess / "session.json") if (sess / "session.json").is_file() else None
    wav = meta is not None and (sess / meta["wav"]).is_file()
    runs = session_runs(lay, sid)
    if not (sdir / "import.json").is_file() or (sdir / "originals.tmp").exists():
        state = "incomplete"
    elif runs:
        state = "clips"
    elif _seg_dirs(sdir, "manifest.jsonl"):
        state = "segmented"
    elif list(sdir.glob("*/probs.json")):
        state = "probs"
    elif meta is not None:
        state = "assembled"
    else:
        state = "imported"
    return {
        "session_id": sid,
        "name": sid.rsplit(".", 1)[0],
        "state": state,
        "can_process": state != "incomplete" and ((sdir / "originals").is_dir() or wav),
        "duration_s": meta["n_samples"] / meta["sr"] if meta else None,
        "n_clips": len(read_jsonl(runs[0]["seg_dir"] / "clips" / "manifest.jsonl"))
        if runs else None,
        "error": None,
    }


def list_sessions(lay: Layout) -> list[dict]:
    """One broken session dir (hand-copied data/, half a restore) must not hide the others."""
    out = []
    for sdir in sorted(lay.sessions.iterdir()) if lay.sessions.is_dir() else []:
        if not sdir.is_dir() or not SESSION_ID_RE.fullmatch(sdir.name):
            continue
        try:
            out.append(_session_row(lay, sdir))
        except (OSError, ValueError, KeyError, TypeError, RunError) as exc:
            out.append({"session_id": sdir.name, "name": sdir.name.rsplit(".", 1)[0],
                        "state": "incomplete", "can_process": False, "duration_s": None,
                        "n_clips": None, "error": f"{type(exc).__name__}: {exc}"})
    return out


def resolve_clip(seg_dir: Path, audio_path: str) -> Path | None:
    """S6: the clip file only if its realpath stays inside the run dir and is a regular file."""
    if os.path.isabs(audio_path):
        return None
    base = os.path.realpath(seg_dir)
    p = os.path.realpath(os.path.join(base, audio_path))
    if os.path.commonpath([base, p]) != base or not os.path.isfile(p):
        return None
    return Path(p)


def clip_table(lay: Layout, seg_dir: Path) -> list[dict]:
    rel = seg_dir.relative_to(lay.sessions)
    rows = read_jsonl(seg_dir / "clips" / "manifest.jsonl")
    return [
        row | {
            "clip_id": hashlib.sha256(f"{rel}/{i}".encode()).hexdigest()[:16],
            "n": i,
            "path": resolve_clip(seg_dir, str(row.get("audio_path") or "")),
        }
        for i, row in enumerate(rows)
    ]


BACKUP_RE = re.compile(r"voxcut-backup_.+\.tar")


def _source_names(lay: Layout) -> list[str]:
    """inbox/ itself (if it holds WAV files) and every first-level subfolder (review focus 1)."""
    entries = sorted(lay.inbox.iterdir()) if lay.inbox.is_dir() else []
    root = [""] if any(p.is_file() and p.suffix.lower() == ".wav" for p in entries) else []
    return root + [p.name for p in entries if p.is_dir() and not p.name.startswith(".")]


def _source_dir(lay: Layout, source: str) -> Path:
    if source not in _source_names(lay):
        raise SessionError(f"нет такого источника во входящих: {source!r}")
    return lay.inbox / source


def inbox_inventory(lay: Layout) -> dict:
    sources = []
    for source in _source_names(lay):
        src = str(lay.inbox / source)
        entry = {"source": source, "chains": [], "error": None}
        try:
            for n, files in enumerate(audio.list_chains(src), 1):
                plan = audio.plan_session(src, n)
                entry["chains"].append({
                    "chain": n,
                    "files": files,
                    "start_local": plan.real_start.isoformat(),
                    "duration_s": plan.n_total / plan.sr,
                    "session_id": plan.session_id,
                    "imported": (lay.sessions / plan.session_id / "import.json").is_file(),
                })
        except (SessionError, SourceError, OSError, RuntimeError) as exc:
            entry["chains"], entry["error"] = [], str(exc)
        sources.append(entry)
    backups = sorted(p.name for p in lay.inbox.iterdir()
                     if p.is_file() and BACKUP_RE.fullmatch(p.name)) if lay.inbox.is_dir() else []
    return {"sources": sources, "backups": backups}


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def import_originals(lay: Layout, source: str, chain: int, start: str | None, log) -> dict:
    """U4, D39: inbox/ -> data/sessions/<id>/originals/, unchanged; every refusal before the move."""
    src = _source_dir(lay, source)
    plan = audio.plan_session(str(src), chain, start)
    for p in plan.files:
        audio.check_source(str(p))  # D18
    before = {p.name: audio.sha256_file(str(p)) for p in plan.files}
    shas = set(before.values())
    for other in sorted(lay.sessions.glob("*/import.json")):
        known = {f["sha256"]: f["name"] for f in load_json(other)["files"]}
        if set(known) == shas:
            log(f"эти файлы уже импортированы: сессия {other.parent.name}")
            return {"session_id": other.parent.name, "existing": True}
        if shas & set(known):
            same = sorted(n for n, s in before.items() if s in known)
            raise SessionError(f"часть файлов уже в сессии {other.parent.name}: "
                               f"{', '.join(same)}; импорт отменён, файлы на месте")
    sdir = lay.sessions / plan.session_id
    if sdir.exists():
        why = ("originals.tmp: незавершённый импорт" if (sdir / "originals.tmp").exists()
               else "с другими файлами")
        raise SessionError(f"{sdir} уже есть ({why}); ничего не тронуто, разберите каталог вручную")
    tmp = sdir / "originals.tmp"
    tmp.mkdir(parents=True)
    for p in plan.files:
        os.rename(p, tmp / p.name)
    os.replace(tmp, sdir / "originals")
    moved = [sdir / "originals" / p.name for p in plan.files]
    after = {p.name: audio.sha256_file(str(p)) for p in moved}
    if after != before:
        bad = sorted(n for n in before if after.get(n) != before[n])
        raise SessionError(f"sha256 после переноса не совпадает: {', '.join(bad)}; "
                           f"файлы оставлены в {sdir / 'originals'}")
    again = audio.plan_session(str(sdir / "originals"), None, start)
    if again.session_id != plan.session_id:
        raise SessionError(f"после переноса сессия {again.session_id}, ожидалась "
                           f"{plan.session_id}; файлы оставлены в {sdir / 'originals'}")
    _write_json(sdir / "import.json", {
        "files": [{"name": p.name, "sha256": before[p.name], "size_bytes": p.stat().st_size}
                  for p in moved],
        "chain": chain, "start": start, "source": source,
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    log(f"импортировано {len(moved)} файлов: сессия {plan.session_id}")
    return {"session_id": plan.session_id, "existing": False}


V1_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "v1.toml"  # U8: the only config


def process_session(lay: Layout, sid: str, stage, log) -> Path:
    """U8: the missing steps assemble -> probs -> segment -> export; resumes after a failure."""
    sdir = lay.sessions / sid
    if not SESSION_ID_RE.fullmatch(sid) or not (sdir / "import.json").is_file():
        raise RunError(f"нет сессии {sid}")
    sess = sdir / f"{sid}.session"
    meta = load_json(sess / "session.json") if (sess / "session.json").is_file() else None
    if meta is None or not (sess / meta["wav"]).is_file():
        if not (sdir / "originals").is_dir():
            raise RunError("сессия из light-бэкапа: модель не прогнать, нужен full-бэкап")
        stage("assemble")
        got = audio.assemble_session(str(sdir / "originals"), str(sdir), None,
                                     load_json(sdir / "import.json")["start"], log)
        if got != sess:
            raise RunError(f"сборка дала {got.name}, ожидалась {sess.name}")
        meta = load_json(sess / "session.json")
    stage("probs")
    pdir = audio.compute_probs(str(sess / meta["wav"]), str(sdir))
    stage("segment")
    seg = segment(str(pdir), *read_config(str(V1_CONFIG)))
    if not (seg / "clips").exists():
        stage("export")
        audio.export_clips(str(seg))
    return seg / "clips"
