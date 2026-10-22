import io
import json
import os
import tarfile
from datetime import datetime

import pytest

from voxcut import audio
from voxcut.audio import RunError, sha256_file
from voxcut.journal import Journal
from voxcut.library import Layout, make_backup, restore_backup

SID = "2026-08-11_21-30_4h16m.0badface"
NOW = datetime(2026, 10, 21, 10, 0)


def make_data(lay, sid=SID, seed=b"a"):
    s = lay.sessions / sid
    (s / "originals").mkdir(parents=True)
    (s / "originals" / "20250301082921.WAV").write_bytes(b"orig" + seed)
    (s / "import.json").write_text("{}")
    sess = s / f"{sid}.session"
    sess.mkdir()
    (sess / "session.json").write_text(json.dumps({"wav": "x.wav", "n_samples": 1, "sr": 1}))
    (sess / "x.wav").write_bytes(b"wav" + seed)
    clips = s / "x.c0.raw" / "seg-aaaaaaaa" / "clips"
    clips.mkdir(parents=True)
    (clips / "0000.wav").write_bytes(b"clip" + seed)
    (clips / "manifest.jsonl").write_text("{}\n")
    (lay.data / "pilot").mkdir(exist_ok=True)
    (lay.data / "pilot" / "labels.txt").write_text("1\t2\tme\n")
    Journal(lay.journal).append({"event_id": "a" * 32, "kind": "idea", "value": "идея"})


def tree(root):
    return {str(p.relative_to(root)): sha256_file(str(p)) for p in sorted(root.rglob("*"))
            if p.is_file()}


@pytest.fixture
def lay(tmp_path):
    lay = Layout(tmp_path / "a")
    lay.ensure()
    return lay


@pytest.fixture
def lay2(tmp_path):
    lay = Layout(tmp_path / "b")
    lay.ensure()
    return lay


def to_inbox(lay, src_lay, name):
    os.link(src_lay.backups / name, lay.inbox / name)


def test_backup_full_light_contents(lay):
    make_data(lay)
    full = make_backup(lay, "full", NOW)
    assert full == "voxcut-backup_2026-10-21_10-00_full.tar"
    light = make_backup(lay, "light", NOW)
    with tarfile.open(lay.backups / full) as t:
        names = set(t.getnames())
        meta = json.load(t.extractfile("backup.json"))
    assert f"data/sessions/{SID}/originals/20250301082921.WAV" in names
    assert f"data/sessions/{SID}/{SID}.session/x.wav" in names
    assert "data/journal/journal.jsonl" in names and "data/pilot/labels.txt" in names
    assert meta["mode"] == "full" and set(meta["files"]) == {n for n in names if n != "backup.json"}
    assert meta["files"] == {f"data/{k}": v for k, v in tree(lay.data).items()}
    with tarfile.open(lay.backups / light) as t:
        lnames = set(t.getnames())
    assert not any("/originals/" in n for n in lnames)
    assert f"data/sessions/{SID}/{SID}.session/x.wav" not in lnames
    assert f"data/sessions/{SID}/{SID}.session/session.json" in lnames
    assert not list(lay.backups.glob("*.tmp"))
    with pytest.raises(RunError, match="уже есть"):
        make_backup(lay, "full", NOW)


def test_restore_into_empty_root(lay, lay2):
    make_data(lay)
    name = make_backup(lay, "full", NOW)
    to_inbox(lay2, lay, name)
    res = restore_backup(lay2, name, lay2.run / "journal.in")
    assert res == {"added": [SID], "skipped": []}
    want = {k: v for k, v in tree(lay.data).items() if not k.startswith("journal/")}
    assert {k: v for k, v in tree(lay2.data).items() if not k.startswith("journal/")} == want
    assert (lay2.run / "journal.in").read_bytes() == lay.journal.read_bytes()
    assert not (lay2.run / "restore.tmp").exists()
    assert (lay2.inbox / name).exists()  # the owner's archive is never removed


def test_restore_interrupted_never_publishes_probs_json_alone(lay, lay2, monkeypatch):
    """probs.json marks a finished probs dir: it goes into data/ after probs.npy."""
    make_data(lay)
    raw = lay.sessions / SID / "x.c0.raw"
    (raw / "probs.npy").write_bytes(b"npy")
    (raw / "probs.json").write_text("{}")
    name = make_backup(lay, "full", NOW)
    to_inbox(lay2, lay, name)
    real = os.replace

    def replace(src, dst):
        if os.path.basename(dst) == "probs.npy":
            raise SystemExit(143)  # SIGTERM between two moves
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(SystemExit):
        restore_backup(lay2, name, lay2.run / "journal.in")
    assert not (lay2.sessions / SID / "x.c0.raw" / "probs.json").exists()


def _evil_tar(lay, kind):
    name = "voxcut-backup_2026-10-21_10-00_full.tar"
    files = {}
    with tarfile.open(lay.inbox / name, "w") as t:
        def add(info, data=b""):
            if info.isfile():
                info.size = len(data)
                files[info.name] = __import__("hashlib").sha256(data).hexdigest()
            t.addfile(info, io.BytesIO(data) if info.isfile() else None)

        add(tarfile.TarInfo("data/ok.txt"), b"ok")
        evil = tarfile.TarInfo({"absolute": "/tmp/evil", "dotdot": "data/../../evil",
                                "symlink": "data/link", "hardlink": "data/hard"}[kind])
        if kind == "symlink":
            evil.type, evil.linkname = tarfile.SYMTYPE, "/etc/passwd"
            t.addfile(evil)
        elif kind == "hardlink":
            evil.type, evil.linkname = tarfile.LNKTYPE, "data/ok.txt"
            t.addfile(evil)
        else:
            add(evil, b"x")
        meta = json.dumps({"mode": "full", "created": "x", "files": files}).encode()
        add(tarfile.TarInfo("backup.json"), meta)
    return name


@pytest.mark.parametrize("kind", ["absolute", "dotdot", "symlink", "hardlink"])
def test_restore_refuses_evil_members(lay, kind):
    name = _evil_tar(lay, kind)
    before = tree(lay.root)
    with pytest.raises(RunError, match="член"):
        restore_backup(lay, name, lay.run / "journal.in")
    assert tree(lay.root) == before
    assert not (lay.run / "restore.tmp").exists()


def test_restore_unknown_name_refused(lay):
    (lay.backups / "voxcut-backup_x_full.tar").write_bytes(b"")
    for name in ("../backups/voxcut-backup_x_full.tar", "voxcut-backup_x_full.tar", "x.tar"):
        with pytest.raises(RunError, match="нет архива"):
            restore_backup(lay, name, lay.run / "journal.in")


def test_restore_diverging_session_refused_before_any_move(lay, lay2):
    make_data(lay)
    name = make_backup(lay, "full", NOW)
    make_data(lay2, seed=b"b")  # same session id, different bytes
    (lay2.data / "pilot" / "labels.txt").unlink()  # one file restore could add
    to_inbox(lay2, lay, name)
    before = tree(lay2.data)
    with pytest.raises(RunError, match="x.wav"):
        restore_backup(lay2, name, lay2.run / "journal.in")
    assert tree(lay2.data) == before


def test_restore_same_session_skipped(lay, lay2):
    make_data(lay)
    name = make_backup(lay, "full", NOW)
    to_inbox(lay2, lay, name)
    restore_backup(lay2, name, lay2.run / "journal.in")
    again = restore_backup(lay2, name, lay2.run / "journal.in")
    assert again == {"added": [], "skipped": [SID]}


def test_restore_full_after_light_adds_originals(lay, lay2):
    make_data(lay)
    light = make_backup(lay, "light", NOW)
    full = make_backup(lay, "full", datetime(2026, 10, 21, 10, 1))
    to_inbox(lay2, lay, light)
    to_inbox(lay2, lay, full)
    restore_backup(lay2, light, lay2.run / "journal.in")
    assert not (lay2.sessions / SID / "originals").exists()
    assert restore_backup(lay2, full, lay2.run / "journal.in")["added"] == [SID]
    assert (lay2.sessions / SID / "originals" / "20250301082921.WAV").read_bytes() == b"origa"


def test_restore_sha_checked_before_extract(lay, lay2):
    make_data(lay)
    name = make_backup(lay, "full", NOW)
    raw = bytearray((lay.backups / name).read_bytes())
    pos = raw.index(b"clipa")
    raw[pos:pos + 5] = b"clipZ"
    (lay2.inbox / name).write_bytes(raw)
    with pytest.raises(RunError, match="sha256"):
        restore_backup(lay2, name, lay2.run / "journal.in")
    assert not (lay2.run / "restore.tmp").exists()
    assert tree(lay2.data) == {}


@pytest.mark.parametrize("case", ["originals_tmp", "symlink"])
def test_full_backup_refuses_recordings_it_would_skip(lay, tmp_path, case):
    """Review #3: «full» must not silently leave out the only copies of recordings."""
    make_data(lay)
    s = lay.sessions / SID
    if case == "originals_tmp":
        (s / "originals.tmp").mkdir()
        (s / "originals.tmp" / "20250301092922.WAV").write_bytes(b"orig")
    else:
        (tmp_path / "elsewhere.WAV").write_bytes(b"orig")
        (s / "originals" / "20250301092922.WAV").symlink_to(tmp_path / "elsewhere.WAV")
    with pytest.raises(RunError, match="20250301092922.WAV"):
        make_backup(lay, "full", NOW)
    assert list(lay.backups.iterdir()) == []
    assert make_backup(lay, "light", NOW)  # light leaves originals out by design


def test_backup_tmp_not_published_on_error(lay, monkeypatch):
    make_data(lay)
    calls = []

    def boom(p):
        calls.append(p)
        if len(calls) == 3:
            raise OSError("диск полон")
        return sha256_file(p)

    monkeypatch.setattr(audio, "sha256_file", boom)
    with pytest.raises(OSError):
        make_backup(lay, "full", NOW)
    assert list(lay.backups.iterdir()) == []


def test_backup_waits_for_journal_writer(lay):
    """The server may be mid-append: the archive gets whole lines only (flock LOCK_SH)."""
    import fcntl
    import threading

    make_data(lay)
    names = []
    with open(lay.journal, "ab") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(b'{"event_id": "' + b"b" * 32)
        f.flush()
        th = threading.Thread(target=lambda: names.append(make_backup(lay, "full", NOW)))
        th.start()
        th.join(0.5)
        assert th.is_alive(), "backup read the journal while a line was half written"
        f.write(b'", "kind": "idea", "value": "", "ts": "2026-10-21T10:00:00+03:00"}\n')
        f.flush()
    th.join()
    with tarfile.open(lay.backups / names[0]) as t:
        lines = t.extractfile("data/journal/journal.jsonl").read().decode().splitlines()
    assert [json.loads(x)["event_id"] for x in lines] == ["a" * 32, "b" * 32]


def test_restore_writes_empty_journal_when_archive_has_none(lay, lay2):
    (lay.data / "pilot").mkdir()
    (lay.data / "pilot" / "a.txt").write_text("x")
    name = make_backup(lay, "light", NOW)
    to_inbox(lay2, lay, name)
    restore_backup(lay2, name, lay2.run / "journal.in")
    assert (lay2.run / "journal.in").read_bytes() == b""


def test_unrestorable_backup_not_published(lay):
    make_data(lay)
    clips = lay.sessions / SID / "x.c0.raw" / "seg-aaaaaaaa" / "clips"
    os.link(clips / "0000.wav", clips / "0001.wav")  # tar writes the 2nd as a hard link
    with pytest.raises(RunError, match="член"):
        make_backup(lay, "full", NOW)
    assert list(lay.backups.iterdir()) == []
