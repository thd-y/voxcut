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
    pending = (sdir / "import.pending.json").is_file() and not (sdir / "import.json").is_file()
    error = None
    if state == "incomplete" and any(sdir.glob("originals*/*")):
        error = (f"незавершённый импорт: файлы записи внутри {sdir.name}, не удаляйте их; "
                 + ("«Обработать» сверит sha256 и завершит импорт" if pending
                    else "нужна ручная проверка"))
    return {
        "session_id": sid,
        "name": sid.rsplit(".", 1)[0],
        "state": state,
        "can_process": (pending and any(sdir.glob("originals*/*"))) or (state != "incomplete"
                                   and ((sdir / "originals").is_dir() or wav)),
        "duration_s": meta["n_samples"] / meta["sr"] if meta else None,
        "n_clips": len(read_jsonl(runs[0]["seg_dir"] / "clips" / "manifest.jsonl"))
        if runs else None,
        "error": error,
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
        raise SessionError(f"нет такого источника во входящих: {source!r}")
    return lay.inbox / source


def inbox_inventory(lay: Layout) -> dict:
    sources = []
    for source in _source_names(lay):
        src = str(lay.inbox / source)
        entry = {"source": source, "chains": [], "error": None}
        try:
            for n, plan in enumerate(audio.plan_chains(src), 1):
                entry["chains"].append({
                    "chain": n,
                    "files": [p.name for p in plan.files],
                    "start_local": plan.real_start.isoformat(),
                    "duration_s": plan.n_total / plan.sr,
                    "session_id": plan.session_id,
                    "imported": (lay.sessions / plan.session_id / "import.json").is_file(),
                })
        except (SessionError, SourceError, OSError, RuntimeError) as exc:
            entry["chains"], entry["error"] = [], str(exc)
        sources.append(entry)
    return {"sources": sources, "backups": _backup_names(lay)}


def _backup_names(lay: Layout) -> list[str]:
    entries = lay.inbox.iterdir() if lay.inbox.is_dir() else []
    return sorted(p.name for p in entries if p.is_file() and BACKUP_RE.fullmatch(p.name))


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

    `files` pins the chain the owner clicked: numbers shift once an earlier chain leaves inbox/."""
    src = _source_dir(lay, source)
    if files is not None:
        chains = audio.list_chains(str(src))
        if files not in chains:
            raise SessionError(f"цепочки {', '.join(files)} нет во входящих (уже импортирована "
                               f"или файлы изменились); ничего не тронуто")
        chain = chains.index(files) + 1
    plan = audio.plan_session(str(src), chain, start)
    if files is not None and [p.name for p in plan.files] != files:  # inbox changed in between
        raise SessionError("входящие изменились во время импорта; ничего не тронуто, повторите")
    for p in plan.files:
        if p.is_symlink():  # os.rename would move the link, not the recording
            raise SessionError(f"{p.name}: ссылка, а не файл; положите во входящие сам файл "
                               f"записи; ничего не тронуто")
        audio.check_source(str(p))  # D18
    before = {p.name: audio.sha256_file(str(p)) for p in plan.files}
    shas = set(before.values())
    for other in sorted(lay.sessions.glob("*/import.json")):
        if not SESSION_ID_RE.fullmatch(other.parent.name):
            continue
        try:
            known = {f["sha256"]: f["name"] for f in load_json(other)["files"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:  # as list_sessions: one bad dir
            log(f"{other.parent.name}: import.json не читается ({type(exc).__name__}), "
                f"проверка повторного импорта для неё пропущена")
            continue
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
        raise SessionError(f"{sdir} уже есть ({why}); ничего не тронуто. Если внутри файлы "
                           f"записи, не удаляйте их: это единственные копии")
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
    if source:  # D50: only an empty source folder goes; inbox/ itself and any file stay
        try:
            os.rmdir(src)
        except OSError:
            if not _has_wav(src):  # WAVs left = another chain to import: never say «удалите»
                try:
                    names = sorted(p.name for p in src.iterdir())
                except OSError:
                    names = []
                log(f"в inbox/{source}/ остались файлы: {', '.join(names)}; "
                    f"папка скрыта, удалите вручную")
    return {"session_id": plan.session_id, "existing": False}


def _rollback_import(sdir: Path, moved: list[Path], failed: Path, exc: OSError) -> None:
    """Move the files back to inbox/; drop our own session dir only if all of them made it."""
    tmp = sdir / "originals.tmp"
    stuck = []
    for p in reversed(moved):
        try:
            os.rename(tmp / p.name, p)
        except OSError:
            stuck.append(p.name)
    if stuck:
        raise SessionError(f"{failed.name}: перенос не удался ({exc}); вернуть не удалось: "
                           f"{', '.join(sorted(stuck))} — они в {tmp}, не удаляйте их: это "
                           f"единственные копии; остальные файлы во входящих") from exc
    tmp.rmdir()
    (sdir / "import.pending.json").unlink()
    sdir.rmdir()
    raise SessionError(f"{failed.name}: перенос не удался ({exc}); файлы во входящих, "
                       f"ничего не тронуто") from exc


def finish_import(lay: Layout, sid: str, log) -> None:
    """Second half of the import, also after an interruption: sha256 after the move vs pending."""
    sdir = lay.sessions / sid
    if (sdir / "import.json").is_file():
        return
    pending = sdir / "import.pending.json"
    if not pending.is_file():
        raise SessionError(f"{sid}: нет import.pending.json, импорт не завершить автоматически")
    meta = load_json(pending)
    if (sdir / "originals.tmp").is_dir() and not (sdir / "originals").exists():
        os.replace(sdir / "originals.tmp", sdir / "originals")
    if (sdir / "originals.tmp").exists():
        raise SessionError(f"{sid}: есть и originals, и originals.tmp; файлы записи внутри, "
                           f"не удаляйте их, нужна ручная проверка")
    before = {f["name"]: f["sha256"] for f in meta["files"]}
    if not any((sdir / d / n).is_file() for d in ("originals", "originals.tmp") for n in before):
        raise SessionError(f"{sid}: файлов записи нет в originals: они во входящих или не "
                           f"восстановлены из бэкапа; ничего не тронуто")
    after = {}
    for name in before:
        p = sdir / "originals" / name
        after[name] = audio.sha256_file(str(p)) if p.is_file() else None
    if after != before:
        bad = sorted(n for n in before if after.get(n) != before[n])
        raise SessionError(f"sha256 после переноса не совпадает: {', '.join(bad)}; "
                           f"файлы оставлены в {sdir / 'originals'}")
    again = audio.plan_session(str(sdir / "originals"), None, meta["start"])
    if again.session_id != sid:
        raise SessionError(f"после переноса сессия {again.session_id}, ожидалась "
                           f"{sid}; файлы оставлены в {sdir / 'originals'}")
    _write_json(sdir / "import.json", meta | {
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    pending.unlink()  # our own bookkeeping file; import.json now holds the same facts
    log(f"импортировано {len(before)} файлов: сессия {sid}")


V1_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "v1.toml"  # U8: the only config


def process_session(lay: Layout, sid: str, stage, log) -> Path:
    """U8: the missing steps assemble -> probs -> segment -> export; resumes after a failure."""
    sdir = lay.sessions / sid
    if not SESSION_ID_RE.fullmatch(sid) or not any(
            (sdir / n).is_file() for n in ("import.json", "import.pending.json")):
        raise RunError(f"нет сессии {sid}")
    if not (sdir / "import.json").is_file():
        stage("import")
        finish_import(lay, sid, log)
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
        raise RunError(f"неизвестный вид бэкапа: {mode}")
    name = f"voxcut-backup_{now:%Y-%m-%d_%H-%M}_{mode}.tar"
    dst = lay.backups / name
    if dst.exists():
        raise RunError(f"{name} уже есть; бэкапы не перезаписываются")
    if mode == "full":  # _backup_files skips these: «full» must not silently lose recordings
        odd = sorted([*lay.sessions.glob("*/originals.tmp/*"),
                      *(p for p in lay.sessions.glob("*/originals/*") if p.is_symlink())])
        if odd:
            raise RunError("full-бэкап отменён: записи вне originals/ или ссылки — "
                           + ", ".join(str(p.relative_to(lay.data)) for p in odd)
                           + " (originals.tmp — незавершённый импорт, обычно его доделывает «Обработать»)")
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
            raise RunError(f"недопустимый член архива: {m.name!r}; ничего не распаковано")


def _verified_files(tar: tarfile.TarFile, name: str) -> dict[str, str]:
    """Members are plain files/dirs under data/ and match backup.json sha256, read from the tar."""
    members = tar.getmembers()
    _check_members(members)
    try:
        files = json.load(tar.extractfile("backup.json"))["files"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError(f"{name}: нет или битый backup.json") from exc
    got = {}
    for m in members:
        if m.isfile() and m.name != "backup.json":
            h, f = hashlib.sha256(), tar.extractfile(m)
            while chunk := f.read(1 << 20):
                h.update(chunk)
            got[m.name] = h.hexdigest()
    if got != files:
        bad = sorted(k for k in set(got) | set(files) if got.get(k) != files.get(k))
        raise RunError(f"{name}: sha256 или состав не совпадает с backup.json: "
                       f"{', '.join(bad[:10])}; ничего не распаковано")
    return files


def restore_backup(lay: Layout, name: str, journal_out: Path) -> dict:
    """U6: verify every member, unpack to .run/restore.tmp, then only add files to data/."""
    if name not in _backup_names(lay):
        raise RunError(f"нет архива во входящих: {name!r}")
    with tarfile.open(lay.inbox / name) as tar:
        files = _verified_files(tar, name)
        tmp = lay.run / "restore.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)  # our own temp dir from an interrupted restore
        tar.extractall(tmp, filter="data")
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
            raise RunError("в data/ уже есть другие версии файлов, ничего не изменено:\n  "
                           + "\n  ".join(conflicts[:20]))
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
