"""Product folders and operations on them (U3–U6). No torch: heavy steps run in jobs."""

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from voxcut import audio
from voxcut import journal as jr
from voxcut.audio import RunError, SourceError, load_json, read_jsonl
from voxcut.cli import read_config, segment
from voxcut.errors import Coded, descriptor
from voxcut.intervals import SessionError, parse_start

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
    pending = (sdir / "import.pending.json").is_file() and not (sdir / "import.json").is_file()
    error = None
    deleting = (sdir / "delete.pending.json").is_file()  # a cut delete: only its retry finishes it
    if deleting:
        state, error = "incomplete", {"code": "session_delete_pending", "params": {"session": sid}}
    elif state == "incomplete" and any(sdir.glob("originals*/*")):
        error = {"code": "session_import_pending" if pending else "session_import_manual",
                 "params": {"session": sdir.name}}
    return {
        "session_id": sid,
        "name": sid.rsplit(".", 1)[0],
        "state": state,
        "can_process": not deleting and ((pending and all(_pending_files(lay, sdir, load_json(
            sdir / "import.pending.json")).values())) or (state != "incomplete"
                                   and ((sdir / "originals").is_dir() or wav))),
        "duration_s": meta["n_samples"] / meta["sr"] if meta else None,
        "n_clips": len(read_jsonl(runs[0]["seg_dir"] / "clips" / "manifest.jsonl"))
        if runs else None,
        "error": error,
    }


def session_row(lay: Layout, sid: str) -> dict | None:
    """One session's row as in list_sessions; None if `sid` is not a session dir."""
    sdir = lay.sessions / sid
    if not SESSION_ID_RE.fullmatch(sid) or not sdir.is_dir():
        return None
    try:
        return _session_row(lay, sdir)
    except (OSError, ValueError, KeyError, TypeError, RunError) as exc:
        return {"session_id": sid, "name": sid.rsplit(".", 1)[0], "state": "incomplete",
                "can_process": False, "duration_s": None, "n_clips": None,
                "error": {"code": "session_unreadable",
                          "params": {"detail": f"{type(exc).__name__}: {exc}"}}}


def list_sessions(lay: Layout) -> list[dict]:
    """One broken session dir (hand-copied data/, half a restore) must not hide the others."""
    names = sorted(p.name for p in lay.sessions.iterdir()) if lay.sessions.is_dir() else []
    return [r for r in (session_row(lay, n) for n in names) if r is not None]


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
    """inbox/ itself and every first-level subfolder, each only if it holds WAV files (D50)."""
    entries = sorted(lay.inbox.iterdir()) if lay.inbox.is_dir() else []
    root = [""] if any(p.is_file() and p.suffix.lower() == ".wav" for p in entries) else []
    return root + [p.name for p in entries if p.is_dir() and not p.name.startswith(".")
                   and _has_wav(p)]


def _has_wav(folder: Path) -> bool:
    try:
        return any(f.is_file() and f.suffix.lower() == ".wav" for f in folder.iterdir())
    except OSError:  # one unreadable folder must not take the whole inbox down
        return False


def _source_dir(lay: Layout, source: str) -> Path:
    if source not in _source_names(lay):
        raise SessionError("inbox_no_source", source=repr(source))
    return lay.inbox / source


START_HINT = "start.txt"  # D94: the corrected start a delete left next to the recordings


def _start_hint(lay: Layout, source: str) -> str | None:
    """The text of inbox/<source>/start.txt if it is a small plain file holding a valid start."""
    f = lay.inbox / source / START_HINT
    try:
        if not source or f.is_symlink() or not f.is_file() or f.stat().st_size > 100:
            return None
        text = f.read_text().strip()
        parse_start(text)
    except (OSError, ValueError):  # SessionError is a ValueError; a bad file is no hint
        return None
    return text


def inbox_inventory(lay: Layout) -> dict:
    sources = []
    for source in _source_names(lay):
        src = str(lay.inbox / source)
        hint = _start_hint(lay, source)
        entry = {"source": source, "chains": [], "error": None}
        try:
            plans = audio.plan_chains(src)
            if len(plans) != 1:  # D94: a delete returns one session, so one chain
                hint = None
            elif hint is not None:  # the id the import with this start makes
                try:
                    plans = [audio.plan_session(src, 1, start=hint)]
                except (SessionError, SourceError, OSError, RuntimeError):  # files changed meanwhile
                    hint = None
            for n, plan in enumerate(plans, 1):
                entry["chains"].append({
                    "chain": n,
                    "files": [p.name for p in plan.files],
                    "start_local": plan.real_start.isoformat(),
                    "duration_s": plan.n_total / plan.sr,
                    "session_id": plan.session_id,
                    "imported": (lay.sessions / plan.session_id / "import.json").is_file(),
                    "start_hint": hint,
                })
        except (SessionError, SourceError, OSError, RuntimeError) as exc:
            entry["chains"], entry["error"] = [], descriptor(exc) if isinstance(exc, Coded) else {
                "code": "inbox_unreadable", "params": {"detail": str(exc)}}
        sources.append(entry)
    return {"sources": sources, "backups": _backup_names(lay)}


def _backup_names(lay: Layout) -> list[str]:
    entries = lay.inbox.iterdir() if lay.inbox.is_dir() else []
    return sorted(p.name for p in entries if p.is_file() and BACKUP_RE.fullmatch(p.name))


def imported_files(lay: Layout) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """D89: file names per inbox source in finished (import.json) and unfinished imports."""
    done: dict[str, set[str]] = {}
    pending: dict[str, set[str]] = {}
    for path in sorted(lay.sessions.glob("*/import*.json")):
        if path.name not in ("import.json", "import.pending.json") or (
                path.name == "import.pending.json" and (path.parent / "import.json").exists()):
            continue
        try:
            meta = load_json(path)
            source, names = meta["source"], {f["name"] for f in meta["files"]}
        except (OSError, ValueError, KeyError, TypeError):  # one bad dir must not take the state down
            continue
        if isinstance(source, str):
            (done if path.name == "import.json" else pending).setdefault(source, set()).update(names)
    return done, pending


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def import_originals(lay: Layout, source: str, chain: int, start: str | None, log,
                     files: list[str] | None = None) -> dict:
    """U4, D39: inbox/ -> data/sessions/<id>/originals/, unchanged; every refusal before the move.

    `files` pins the chain the user clicked: numbers shift once an earlier chain leaves inbox/."""
    src = _source_dir(lay, source)
    if files is not None:
        chains = audio.list_chains(str(src))
        if files not in chains:
            raise SessionError("import_chain_gone", files=files)
        chain = chains.index(files) + 1
    plan = audio.plan_session(str(src), chain, start)
    if files is not None and [p.name for p in plan.files] != files:  # inbox changed in between
        raise SessionError("import_inbox_changed")
    for p in plan.files:
        if p.is_symlink():  # os.rename would move the link, not the recording
            raise SessionError("import_symlink", file=p.name)
        audio.check_source(str(p))  # D18
    before = {p.name: audio.sha256_file(str(p)) for p in plan.files}
    shas = set(before.values())
    unfinished = [p for p in lay.sessions.glob("*/import.pending.json")
                  if not (p.parent / "import.json").exists()]
    for other in sorted([*lay.sessions.glob("*/import.json"), *unfinished]):
        if not SESSION_ID_RE.fullmatch(other.parent.name):
            continue
        try:
            known = {f["sha256"]: f["name"] for f in load_json(other)["files"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:  # as list_sessions: one bad dir
            log(f"{other.parent.name}: import.json is unreadable ({type(exc).__name__}), "
                f"the re-import check skips it", "WARNING")
            continue
        if other.name == "import.pending.json" and shas & set(known):  # D65
            raise SessionError("import_unfinished_elsewhere", session=other.parent.name)
        if set(known) == shas:
            log(f"these files are already imported: session {other.parent.name}")
            return {"session_id": other.parent.name, "existing": True}
        if shas & set(known):
            same = sorted(n for n, s in before.items() if s in known)
            raise SessionError("import_partly_imported", session=other.parent.name, files=same)
    sdir = lay.sessions / plan.session_id
    if sdir.exists():
        raise SessionError("import_dir_unfinished" if (sdir / "originals.tmp").exists()
                           else "import_dir_other", dir=str(sdir))
    sdir.mkdir(parents=True)
    _write_json(sdir / "import.pending.json", {  # lets an interrupted import be finished
        "files": [{"name": p.name, "sha256": before[p.name], "size_bytes": p.stat().st_size}
                  for p in plan.files],
        "chain": chain, "start": start, "source": source,
    })
    tmp = sdir / "originals.tmp"
    tmp.mkdir()
    moved = []
    try:
        for p in plan.files:
            os.rename(p, tmp / p.name)
            moved.append(p)
    except OSError as exc:  # SIGTERM (SystemExit) is not rolled back: «Обработать» finishes it
        _rollback_import(sdir, moved, p, exc)
    os.replace(tmp, sdir / "originals")
    finish_import(lay, plan.session_id, log)
    return {"session_id": plan.session_id, "existing": False}  # finish_import dropped the source


def _drop_empty_source(lay: Layout, source: str, log) -> None:
    """D50: only an empty source folder goes; inbox/ itself and any file stay.

    `source` may come from a restored import.pending.json: only a plain, visible folder name."""
    if not isinstance(source, str) or not source or Path(source).name != source \
            or source.startswith("."):
        return
    src = lay.inbox / source
    if src.is_symlink() or not src.is_dir():
        return
    try:
        os.rmdir(src)
    except OSError:
        if not _has_wav(src):  # WAVs left = another chain to import: never say «удалите»
            try:
                names = sorted(p.name for p in src.iterdir())
            except OSError:
                names = []
            log(f"inbox/{source}/ still has files: {', '.join(names)}; "
                f"the Library no longer shows the folder, delete it by hand", "WARNING")


def _rollback_import(sdir: Path, moved: list[Path], failed: Path, exc: OSError) -> None:
    """Move the files back to inbox/; drop our own session dir only if all of them made it."""
    tmp = sdir / "originals.tmp"
    stuck = []
    for p in reversed(moved):
        if os.path.lexists(p):  # something new in its place, even a dangling link: keep it
            stuck.append(p.name)
            continue
        try:
            os.rename(tmp / p.name, p)
        except OSError:
            stuck.append(p.name)
    if stuck:
        raise SessionError("import_move_stuck", file=failed.name, detail=str(exc),
                           files=sorted(stuck), dir=str(tmp)) from exc
    tmp.rmdir()
    (sdir / "import.pending.json").unlink()
    sdir.rmdir()
    raise SessionError("import_move_failed", file=failed.name, detail=str(exc)) from exc


def _pending_files(lay: Layout, sdir: Path, meta: dict) -> dict[str, Path | None]:
    """D65: where each recording of an unfinished import is now: originals*, inbox/ or nowhere."""
    dirs = [sdir / "originals", sdir / "originals.tmp"]
    if meta.get("source") in _source_names(lay):  # the pending file may come from a backup
        dirs.append(lay.inbox / meta["source"])
    out = {}
    for f in meta["files"]:
        if Path(f["name"]).name != f["name"] or f["name"] in ("", ".", ".."):
            out[f["name"]] = None  # not a plain file name: never looked up outside these dirs
            continue
        out[f["name"]] = next((d / f["name"] for d in dirs if (d / f["name"]).is_file()
                               and not (d / f["name"]).is_symlink()), None)
    return out


def finish_import(lay: Layout, sid: str, log) -> None:
    """Second half of the import, also after an interruption: sha256 after the move vs pending."""
    sdir = lay.sessions / sid
    if (sdir / "import.json").is_file():
        return
    pending = sdir / "import.pending.json"
    if not pending.is_file():
        raise SessionError("finish_no_pending", session=sid)
    meta = load_json(pending)
    if (sdir / "originals.tmp").is_dir() and not (sdir / "originals").exists():
        os.replace(sdir / "originals.tmp", sdir / "originals")
    if (sdir / "originals.tmp").exists():
        raise SessionError("finish_both_dirs", session=sid)
    before = {f["name"]: f["sha256"] for f in meta["files"]}
    where = _pending_files(lay, sdir, meta)
    missing = sorted(n for n, p in where.items() if p is None)
    if len(missing) == len(before):
        raise SessionError("finish_none_in_originals", session=sid)
    if missing:
        raise SessionError("finish_missing", session=sid, files=missing)
    incoming = {n: p for n, p in where.items() if p.parent != sdir / "originals"}
    changed = sorted(n for n, p in incoming.items() if audio.sha256_file(str(p)) != before[n])
    if changed:
        raise SessionError("finish_changed", files=changed)
    (sdir / "originals").mkdir(exist_ok=True)
    for n, p in incoming.items():  # D65, D39: the same rename as the import itself
        try:
            os.rename(p, sdir / "originals" / n)
        except OSError as exc:
            raise SessionError("finish_move_failed", file=n, source=str(meta["source"]),
                               detail=str(exc)) from exc
    after = {}
    for name in before:
        p = sdir / "originals" / name
        after[name] = audio.sha256_file(str(p)) if p.is_file() else None
    if after != before:
        bad = sorted(n for n in before if after.get(n) != before[n])
        raise SessionError("finish_sha_mismatch", files=bad, dir=str(sdir / "originals"))
    again = audio.plan_session(str(sdir / "originals"), None, meta["start"])
    if again.session_id != sid:
        raise SessionError("finish_session_mismatch", got=again.session_id, expected=sid,
                           dir=str(sdir / "originals"))
    _write_json(sdir / "import.json", meta | {
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    pending.unlink()  # our own bookkeeping file; import.json now holds the same facts
    log(f"imported {len(before)} files: session {sid}")
    hint = lay.inbox / str(meta.get("source")) / START_HINT
    if meta.get("source") and Path(str(meta["source"])).name == meta["source"] \
            and not hint.is_symlink() and hint.is_file():
        hint.unlink()  # D94: used up; notes.txt is the user's text and stays
    _drop_empty_source(lay, meta.get("source"), log)  # also after a retry that moved nothing


def _hms(x: float) -> str:
    t = int(x)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _notes_text(lay: Layout, sid: str) -> tuple[str, int]:
    """D93: the session's notes, the latest per span, non-empty, by time in the recording."""
    events = jr.load(lay.journal)[0] if lay.journal.is_file() else []
    last = {}
    for e in jr.session_spans(events, sid):
        if e["kind"] == "note":
            last[(e["start_s"], e["end_s"])] = e["value"]
    notes = sorted((k, v) for k, v in last.items() if v)
    return "".join(f"{_hms(s)}–{_hms(e)}  " + v.replace("\n", "\n    ") + "\n"
                   for (s, e), v in notes), len(notes)


def _files_ok(folder: Path, files: dict[str, str]) -> list[str]:
    """Names of `files` missing in `folder` or with another sha256."""
    return sorted(n for n, sha in files.items()
                  if not (folder / n).is_file() or audio.sha256_file(str(folder / n)) != sha)


def delete_session(lay: Layout, sid: str, log) -> dict:
    """D90, D94: the import in reverse. originals/ -> inbox/<sid>/ unchanged (sha256 before and
    after) with notes.txt and start.txt, then the session dir goes; a re-run finishes a cut one."""
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        raise SessionError("delete_no_session", session=str(sid))
    sdir, gone = lay.sessions / sid, lay.sessions / f"{sid}.tmp"
    dst, tmp = lay.inbox / sid, lay.inbox / f".{sid}.tmp"  # a dot: the inbox never lists it (D50)
    if not sdir.is_dir() and gone.is_dir():  # cut after the session dir was renamed
        pend = gone / "delete.pending.json"  # rmtree may already have taken it
        meta = load_json(pend) if pend.is_file() else {"files": {}, "notes": 0}
        _erase(gone, log)
        return {"session_id": sid, "files": len(meta["files"]), "notes": meta["notes"]}
    if not sdir.is_dir():
        raise SessionError("delete_no_session", session=sid)
    orig, pending = sdir / "originals", sdir / "delete.pending.json"
    if pending.is_file():
        meta = load_json(pending)
        files = meta["files"]
    else:
        if (not (sdir / "import.json").is_file() or (sdir / "import.pending.json").exists()
                or (sdir / "originals.tmp").exists()):
            raise SessionError("delete_incomplete", session=sid)
        imp = load_json(sdir / "import.json")
        files = {f["name"]: f["sha256"] for f in imp["files"]}
        present = {p.name for p in orig.iterdir()} if orig.is_dir() else set()
        if not present:  # D94 p. 5: a light backup's stub holds no recording, only the dir goes
            if gone.exists():
                _erase(gone, log)
            os.replace(sdir, gone)
            _erase(gone, log)
            log(f"session {sid} deleted: it had no recordings (light)")
            return {"session_id": sid, "files": 0, "notes": 0}
        if present != set(files):
            raise SessionError("delete_incomplete", session=sid)
        bad = _files_ok(orig, files)
        if bad:
            raise SessionError("delete_sha_mismatch", files=bad)
        if tmp.exists() or (dst.exists() and not dst.is_dir()) or _has_wav(dst):  # notes only: kept
            raise SessionError("delete_inbox_exists", dir=str(dst))
        text, n = _notes_text(lay, sid)
        carried: dict[str, str] = {}  # what an earlier delete left (notes): kept, never overwritten
        for f in sorted(dst.iterdir()) if dst.is_dir() else []:
            carried[f.name] = _free_name(f.name, {*files, "notes.txt", "start.txt", *carried.values()})
        meta = {"files": files, "start": imp.get("start"), "notes": n, "notes_text": text,
                "carried": carried}
        _write_json(pending, meta)
    if not dst.is_dir() or _files_ok(dst, files):
        tmp.mkdir(exist_ok=True)
        carried = meta.get("carried", {})
        if dst.is_dir():
            for old, new in carried.items():
                if (dst / old).exists():
                    os.rename(dst / old, tmp / new)
            dst.rmdir()  # refuses if something new appeared there meanwhile
        for name in files:
            if (orig / name).is_file():
                try:
                    os.rename(orig / name, tmp / name)  # D39 in reverse: never a copy
                except OSError as exc:
                    raise SessionError("delete_move_failed", file=name, detail=str(exc)) from exc
        if meta["notes_text"]:
            (tmp / "notes.txt").write_text(meta["notes_text"])
        if meta["start"] is not None:
            (tmp / "start.txt").write_text(meta["start"] + "\n")
        bad = _files_ok(tmp, files)
        if bad:
            for name in files:
                if (tmp / name).is_file():
                    os.rename(tmp / name, orig / name)
            for extra in ("notes.txt", "start.txt"):
                (tmp / extra).unlink(missing_ok=True)
            for old, new in carried.items():
                if (tmp / new).exists():
                    os.rename(tmp / new, tmp / old)
            if any(tmp.iterdir()):  # the carried-over files go back where they were
                os.replace(tmp, dst)
            else:
                tmp.rmdir()
            pending.unlink()
            raise SessionError("delete_sha_mismatch", files=bad)
        os.replace(tmp, dst)
    if gone.exists():  # junk of an earlier cut erase; _erase refuses it if a recording is there
        _erase(gone, log)
    os.replace(sdir, gone)
    _erase(gone, log)
    log(f"session {sid} deleted: {len(files)} files moved back to inbox/{sid}/")
    return {"session_id": sid, "files": len(files), "notes": meta["notes"]}


def sweep_deleted(lay: Layout, busy=lambda sid: False) -> tuple[list[str], list[str]]:
    """What a cut delete left: sessions/<sid>.tmp with no sessions/<sid>/ and no live job is erased;
    one still holding a recording stays. Returns (erased sids, messages about the kept ones)."""
    erased, said = [], []
    for gone in sorted(lay.sessions.glob("*.tmp")) if lay.sessions.is_dir() else []:
        sid = gone.name[:-len(".tmp")]
        if (not SESSION_ID_RE.fullmatch(sid) or (lay.sessions / sid).exists() or not gone.is_dir()
                or busy(sid)):  # a delete unit from before the restart may still be erasing it
            continue
        try:
            _erase(gone, lambda _m: None)
            erased.append(sid)
        except (SessionError, OSError) as exc:
            said.append(f"{gone}: {exc}")
    return erased, said


def _free_name(name: str, taken: set[str]) -> str:
    """`name`, or name-1, name-2…, whichever is not taken."""
    p = Path(name)
    i, out = 0, name
    while out in taken:
        i += 1
        out = f"{p.stem}-{i}{p.suffix}"
    return out


def _erase(gone: Path, log) -> None:
    """Hard rule: a session dir goes only with no recording left in originals/."""
    left = sorted(p.name for p in (gone / "originals").iterdir()) if (gone / "originals").is_dir() else []
    if left:
        raise SessionError("delete_originals_left", files=left)
    shutil.rmtree(gone)


V1_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "v1.toml"  # U8: the only config


def process_session(lay: Layout, sid: str, stage, log) -> Path:
    """U8: the missing steps assemble -> probs -> segment -> export; resumes after a failure."""
    sdir = lay.sessions / sid
    if not SESSION_ID_RE.fullmatch(sid) or not any(
            (sdir / n).is_file() for n in ("import.json", "import.pending.json")):
        raise RunError("process_no_session", session=sid)
    if not (sdir / "import.json").is_file():
        stage("import")
        finish_import(lay, sid, log)
    sess = sdir / f"{sid}.session"
    meta = load_json(sess / "session.json") if (sess / "session.json").is_file() else None
    if meta is None or not (sess / meta["wav"]).is_file():
        if not (sdir / "originals").is_dir():
            raise RunError("process_light")
        stage("assemble")
        got = audio.assemble_session(str(sdir / "originals"), str(sdir), None,
                                     load_json(sdir / "import.json")["start"], log)
        if got != sess:
            raise RunError("process_assembled_other", got=got.name, expected=sess.name)
        meta = load_json(sess / "session.json")
    stage("probs")
    pdir = audio.compute_probs(str(sess / meta["wav"]), str(sdir))
    stage("segment")
    seg = segment(str(pdir), *read_config(str(V1_CONFIG)))
    if not (seg / "clips").exists():
        stage("export")
        audio.export_clips(str(seg), str(sess / meta["wav"]))  # D64
    return seg / "clips"


JOURNAL_MEMBER = "data/journal/journal.jsonl"
LAST_ON_RESTORE = ("run.json", "manifest.jsonl", "import.json", "probs.json")  # markers last


def _backup_files(lay: Layout, mode: str) -> list[Path]:
    """Regular files of data/ (no symlinks, no *.tmp); light drops originals and session WAVs."""
    skip = set()
    if mode == "light":
        for sj in lay.sessions.glob("*/*.session/session.json"):
            skip.add(sj.parent / load_json(sj)["wav"])
    out = []
    for p in sorted(lay.data.rglob("*")):
        rel = p.relative_to(lay.data)
        if (p.is_symlink() or not p.is_file() or p == lay.journal or p in skip
                or any(part.endswith(".tmp") for part in rel.parts)
                or (mode == "light" and len(rel.parts) > 2 and rel.parts[0] == "sessions"
                    and rel.parts[2] == "originals")):
            continue
        out.append(p)
    return out


def make_backup(lay: Layout, mode: str, now: datetime) -> str:
    """U6: one uncompressed tar of data/ plus backup.json with sha256 of every member."""
    if mode not in ("full", "light"):
        raise RunError("backup_bad_mode", mode=str(mode))
    name = f"voxcut-backup_{now:%Y-%m-%d_%H-%M}_{mode}.tar"
    dst = lay.backups / name
    if dst.exists():
        raise RunError("backup_exists", name=name)
    if mode == "full":  # _backup_files skips these: «full» must not silently lose recordings
        odd = sorted([*lay.sessions.glob("*/originals.tmp/*"),
                      *(p for p in lay.sessions.glob("*/originals/*") if p.is_symlink())])
        if odd:
            raise RunError("backup_full_refused", files=[str(p.relative_to(lay.data)) for p in odd])
    tmp = lay.backups / (name + ".tmp")
    files: dict[str, str] = {}
    try:
        with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
            for p in _backup_files(lay, mode):
                arc = f"data/{p.relative_to(lay.data)}"
                files[arc] = audio.sha256_file(str(p))  # run dirs are immutable: same bytes
                tar.add(p, arc, recursive=False)
            if lay.journal.is_file():
                with open(lay.journal, "rb") as f:  # the server appends meanwhile: whole lines only
                    fcntl.flock(f, fcntl.LOCK_SH)
                    data = f.read()
                info = tarfile.TarInfo(JOURNAL_MEMBER)
                info.size, info.mtime = len(data), int(now.timestamp())
                tar.addfile(info, io.BytesIO(data))
                files[JOURNAL_MEMBER] = hashlib.sha256(data).hexdigest()
            meta = json.dumps({"created": now.isoformat(), "mode": mode, "files": files},
                              ensure_ascii=False, indent=1).encode()
            info = tarfile.TarInfo("backup.json")
            info.size, info.mtime = len(meta), int(now.timestamp())
            tar.addfile(info, io.BytesIO(meta))
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        with tarfile.open(tmp) as tar:  # a backup that restore would refuse is not published
            _verified_files(tar, name)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)  # our own temp file, never published
        raise
    return name


def _check_members(members: list[tarfile.TarInfo]) -> None:
    for m in members:
        parts = Path(m.name).parts
        ok = ((m.isfile() or m.isdir()) and not m.name.startswith("/") and ".." not in parts
              and (m.name == "backup.json" or (parts[:1] == ("data",) and len(parts) > 1)))
        if not ok:
            raise RunError("restore_bad_member", member=repr(m.name))


def _verified_files(tar: tarfile.TarFile, name: str) -> dict[str, str]:
    """Members are plain files/dirs under data/ and match backup.json sha256, read from the tar."""
    members = tar.getmembers()
    _check_members(members)
    try:
        files = json.load(tar.extractfile("backup.json"))["files"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError("restore_no_manifest", name=name) from exc
    got = {}
    for m in members:
        if m.isfile() and m.name != "backup.json":
            h, f = hashlib.sha256(), tar.extractfile(m)
            while chunk := f.read(1 << 20):
                h.update(chunk)
            got[m.name] = h.hexdigest()
    if got != files:
        bad = sorted(k for k in set(got) | set(files) if got.get(k) != files.get(k))
        raise RunError("restore_mismatch", name=name, files=bad[:10])
    return files


def restore_backup(lay: Layout, name: str, journal_out: Path) -> dict:
    """U6: verify every member, unpack to .run/restore.tmp, then only add files to data/."""
    if name not in _backup_names(lay):
        raise RunError("restore_no_archive", name=repr(name))
    with tarfile.open(lay.inbox / name) as tar:
        files = _verified_files(tar, name)
        tmp = lay.run / "restore.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)  # our own temp dir from an interrupted restore: not used space
        need, free = sum(m.size for m in tar.getmembers()), shutil.disk_usage(lay.run).free
        if need > free:  # S-4
            raise RunError("restore_no_space", name=name, need=-(-need // 2**20),
                           free=free // 2**20)
        data = (lay.root / "data").resolve()
        for rel in files:  # S-5: no dir is created or written through a link out of data/
            parent = (lay.root / rel).parent
            while not os.path.lexists(parent):  # a broken link is not missing
                parent = parent.parent
            if not parent.resolve().is_relative_to(data):
                raise RunError("restore_bad_target", path=rel)
        try:
            tar.extractall(tmp, filter="data")
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)  # a partial unpack would eat the next one's space
            raise
    try:
        rels = sorted((k for k in files if k != JOURNAL_MEMBER and not k.startswith(
            "data/journal/")), key=lambda k: (Path(k).name in LAST_ON_RESTORE, k))
        conflicts, new = [], []
        for rel in rels:
            dst = lay.root / rel
            if not dst.exists():
                new.append(rel)
            elif not dst.is_file() or audio.sha256_file(str(dst)) != files[rel]:
                conflicts.append(rel)
        if conflicts:
            raise RunError("restore_conflicts", lines=[f"  {c}" for c in conflicts[:20]])
        for rel in new:
            (lay.root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp / rel, lay.root / rel)
        if JOURNAL_MEMBER in files:
            shutil.copyfile(tmp / JOURNAL_MEMBER, journal_out)
        else:
            journal_out.write_bytes(b"")  # the merge step always has a file to read
    finally:
        shutil.rmtree(tmp)
    sids = sorted({Path(k).parts[2] for k in rels if k.startswith("data/sessions/")
                   and len(Path(k).parts) > 3})
    added = sorted({Path(k).parts[2] for k in new if k.startswith("data/sessions/")
                    and len(Path(k).parts) > 3})
    return {"added": added, "skipped": [s for s in sids if s not in added]}
