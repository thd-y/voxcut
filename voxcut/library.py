"""Product folders and operations on them (U3–U6). No torch: heavy steps run in jobs."""

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from voxcut.audio import load_json, read_jsonl

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


def list_sessions(lay: Layout) -> list[dict]:
    out = []
    for sdir in sorted(lay.sessions.iterdir()) if lay.sessions.is_dir() else []:
        sid = sdir.name
        if not sdir.is_dir() or not SESSION_ID_RE.fullmatch(sid):
            continue
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
        out.append({
            "session_id": sid,
            "name": sid.rsplit(".", 1)[0],
            "state": state,
            "can_process": state != "incomplete" and ((sdir / "originals").is_dir() or wav),
            "duration_s": meta["n_samples"] / meta["sr"] if meta else None,
            "n_clips": len(read_jsonl(runs[0]["seg_dir"] / "clips" / "manifest.jsonl"))
            if runs else None,
        })
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
