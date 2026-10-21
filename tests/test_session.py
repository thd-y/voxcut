import json
import os
import shutil
import struct
from datetime import datetime, timedelta

import numpy as np
import pytest
import soundfile as sf

from voxcut import audio
from voxcut.audio import (
    RunError,
    SourceError,
    assemble_session,
    compute_probs,
    export_clips,
    sha256_file,
)
from voxcut.cli import main, segment
from voxcut.intervals import (
    RIFF_MAX,
    SEAM_TOL_S,
    SessionError,
    at_seam,
    data_bytes_ok,
    file_start,
    locate,
    meta_key,
    mtime_warnings,
    parse_config,
    parse_start,
    pick_chain,
    seam_frames,
    seam_windows,
    session_name,
    split_chains,
    start_local,
)

SR = 16000
T0 = datetime(2025, 3, 1, 8, 29, 21)
HOUR = 3600 * SR


@pytest.mark.parametrize("name, expected", [
    ("20250301082921.WAV", T0),
    ("20250301082921.wav", T0),
    ("20260811193015.WAV", datetime(2026, 8, 11, 19, 30, 15)),
    ("20250231082921.WAV", None),  # 31 февраля
    ("20250301256000.WAV", None),  # 25 часов
    ("2025030108292.WAV", None),
    ("20250301082921.flac", None),
    ("REC001.WAV", None),
    ("x20250301082921.WAV", None),
])
def test_file_start(name, expected):
    assert file_start(name) == expected


@pytest.mark.parametrize("n_samples, expected", [
    (245_512_192, "2025-03-01_08-29_4h16m"),
    (int(59.5 * SR), "2025-03-01_08-29_0h01m"),  # половина вверх
    (int(29.9 * SR), "2025-03-01_08-29_0h00m"),
    (int(30 * SR), "2025-03-01_08-29_0h01m"),  # не банковское: 0.5 мин -> 1
    (int(150 * SR), "2025-03-01_08-29_0h03m"),  # 2.5 мин -> 3, банковское дало бы 2
    (HOUR * 10, "2025-03-01_08-29_10h00m"),
])
def test_session_name(n_samples, expected):
    name = session_name(T0, n_samples, SR)
    assert name == expected
    assert ":" not in name and " " not in name


def parts(*gaps_s, n=HOUR):
    """Files of length n with the given gaps (s) between end and next start."""
    out, t = [], T0
    for i, gap in enumerate((0, *gaps_s)):
        t = t + timedelta(seconds=gap)
        out.append((f"f{i}", t, n))
        t = t + timedelta(seconds=n / SR)
    return out


@pytest.mark.parametrize("gap", [-2.0, 0, 0.87, 1.87, 5.0])
def test_split_one_chain(gap):
    assert split_chains(parts(gap, gap), SR) == [[0, 1, 2]]


@pytest.mark.parametrize("gap", [5.01, 600, 1800])
def test_pause_refused(gap):
    with pytest.raises(SessionError, match="склейка пауз пока не поддерживается") as e:
        split_chains(parts(1.0, gap), SR)
    assert "f1" in str(e.value) and "f2" in str(e.value)


def test_long_gap_gives_separate_chains():
    assert split_chains(parts(1.0, 1800.01, 1.0), SR) == [[0, 1], [2, 3]]


def test_overlap_refused():
    with pytest.raises(SessionError, match="перекрываются"):
        split_chains(parts(-2.01), SR)


def test_split_sorts_by_start():
    p = parts(1.0, 1.0)
    assert split_chains([p[2], p[0], p[1]], SR) == [[1, 2, 0]]


def test_pick_chain():
    names = ["a", "b", "c"]
    assert pick_chain([[0, 1, 2]], None, names) == [0, 1, 2]
    assert pick_chain([[0], [1, 2]], 2, names) == [1, 2]
    with pytest.raises(SessionError, match="--chain") as e:
        pick_chain([[0], [1, 2]], None, names)
    assert "1: a" in str(e.value) and "2: b, c" in str(e.value)
    for bad in (0, 3):
        with pytest.raises(SessionError, match="вне диапазона"):
            pick_chain([[0], [1, 2]], bad, names)


def test_meta_key():
    files = [("a.WAV", 100, 10), ("b.WAV", 200, 20)]
    key = meta_key(files, None)
    assert len(key) == 8 and key == meta_key(list(files), None)
    assert key != meta_key(files, "2025-03-01 09:00")
    assert key != meta_key(files[:1], None)
    assert key != meta_key([("a.WAV", 100, 11), files[1]], None)


def test_data_bytes_ok():
    # 2 канала int16: 4 байта на кадр; заголовок 44 байта
    limit = (RIFF_MAX - 44) // 4
    assert data_bytes_ok(limit, 2, 2)
    assert not data_bytes_ok(limit + 1, 2, 2)
    assert data_bytes_ok(RIFF_MAX - 44, 1, 1)
    assert not data_bytes_ok(RIFF_MAX - 43, 1, 1)


@pytest.mark.parametrize("text, expected", [
    ("2025-03-01 09:00", datetime(2025, 3, 1, 9, 0)),
    ("2025-03-01 09:00:05", datetime(2025, 3, 1, 9, 0, 5)),
])
def test_parse_start(text, expected):
    assert parse_start(text) == expected


@pytest.mark.parametrize("text", ["2025-03-01", "09:00", "2025-03-01T09:00", "2025-02-30 09:00"])
def test_parse_start_bad(text):
    with pytest.raises(SessionError, match="2025-03-01 09:00"):
        parse_start(text)


def test_locate():
    offsets = [0, 100, 250]
    assert locate(0, offsets) == (0, 0)
    assert locate(99, offsets) == (0, 99)
    assert locate(100, offsets) == (1, 0)
    assert locate(300, offsets) == (2, 50)


def test_start_local():
    assert start_local(T0, 0, SR, 0) == "2025-03-01T08:29:21"
    assert start_local(T0, 90 * SR, SR, 0) == "2025-03-01T08:30:51"
    # --start: настоящее начало на 3 ч 2 с позже часов диктофона
    assert start_local(T0, 90 * SR, SR, 3 * 3600 + 2) == "2025-03-01T11:30:53"


SEAM = 10 * SR
TOL = int(SEAM_TOL_S * SR)


@pytest.mark.parametrize("start, end, expected", [
    (SEAM - 3 * SR, SEAM + 3 * SR, True),  # реплика через стык
    (SEAM - 3 * SR, SEAM - SR // 2, True),  # кончается за 0.5 с до стыка
    (SEAM + SR // 2, SEAM + 3 * SR, True),  # начинается через 0.5 с после
    (SEAM - 5 * SR, SEAM - 2 * SR, False),  # кончается за 2 с
    (SEAM + 2 * SR, SEAM + 5 * SR, False),  # начинается через 2 с
    (SEAM - 3 * SR, SEAM - TOL, False),  # [s, e) касается окна [seam − T, ...) снаружи
    (SEAM + TOL, SEAM + 3 * SR, False),  # окно полуоткрытое: seam + T уже вне
    (SEAM + TOL - 1, SEAM + 3 * SR, True),
])
def test_at_seam(start, end, expected):
    assert at_seam(start, end, [SEAM], TOL) is expected


def test_at_seam_no_seams():
    assert at_seam(0, 100, [], TOL) is False


def test_seam_windows_clipped():
    assert seam_windows([SEAM, SR // 2, 20 * SR - 10], TOL, 20 * SR) == [
        (SEAM - TOL, SEAM + TOL), (0, SR // 2 + TOL), (20 * SR - 10 - TOL, 20 * SR)]


@pytest.mark.parametrize("offsets, sr, expected", [
    ([57_602_048, 230_407_168], SR, [112_504, 450_014]),  # стыки кратны кадру
    ([2 * SR, 4 * SR], SR, [62, 125]),  # стык внутри кадра: сброс перед этим кадром
    ([3 * 48_000], 48_000, [93]),  # смещение в сэмплах источника, кадры в 16 кГц
    ([0, 511, 512], SR, [1]),  # кадр 0 и так начинается со сброса
])
def test_seam_frames(offsets, sr, expected):
    assert seam_frames(offsets, sr) == expected


def test_mtime_warnings():
    p = parts(1.0, 1.0)
    ends = [t + timedelta(seconds=n / SR) for _, t, n in p]
    assert mtime_warnings(p, ends, SR) == []
    assert mtime_warnings(p, [e + timedelta(seconds=4.9) for e in ends], SR) == []
    swapped = [ends[1], ends[0], ends[2]]
    w = mtime_warnings(p, swapped, SR)
    assert any("порядок" in x for x in w)
    w = mtime_warnings(p, [ends[0], ends[1] + timedelta(seconds=5.1), ends[2]], SR)
    assert len(w) == 1 and "f1" in w[0]


# --- assemble_session (audio.py) ---



def make_folder(d, starts, seconds=2.0, channels=2, data=None, seed=0):
    """Recorder files: name = start, mtime = end. data(i, n) -> int16 array overrides noise."""
    d.mkdir(exist_ok=True)
    rng = np.random.default_rng(seed)
    paths = []
    for i, t in enumerate(starts):
        n = int(seconds * SR)
        x = data(i, n) if data else rng.integers(-20000, 20000, (n, channels), dtype=np.int16)
        p = d / f"{t:%Y%m%d%H%M%S}.WAV"
        sf.write(p, x, SR, subtype="PCM_16")
        end = (t + timedelta(seconds=n / SR)).timestamp()
        os.utime(p, (end, end))
        paths.append(p)
    return paths


def chain_starts(k, t=T0, seconds=2.0, gap=1.0):
    return [t + timedelta(seconds=i * (seconds + gap)) for i in range(k)]


@pytest.fixture
def no_decode(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("декод ffmpeg не должен вызываться")
    monkeypatch.setattr(audio, "count_samples", fail)


def test_foreign_name_refused(tmp_path, no_decode):
    make_folder(tmp_path / "in", chain_starts(2))
    sf.write(tmp_path / "in" / "REC001.WAV", np.zeros((10, 2), np.int16), SR)
    with pytest.raises(SessionError, match="YYYYMMDDHHMMSS.wav.*REC001.WAV"):
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=lambda m: None)
    assert not (tmp_path / "out").exists()


def test_broken_wav_refused_before_chains(tmp_path, no_decode):
    paths = make_folder(tmp_path / "in", chain_starts(2) + [T0 + timedelta(hours=5)])
    raw = bytearray(paths[1].read_bytes())
    pos = raw.index(b"data")
    raw[pos + 4:pos + 8] = struct.pack("<I", 0)
    paths[1].write_bytes(raw)
    with pytest.raises(SourceError, match=f"{paths[1].name}.*битая структура"):
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=lambda m: None)


def test_two_chains_refused_with_numbers(tmp_path, no_decode):
    make_folder(tmp_path / "in", chain_starts(2) + chain_starts(1, T0 + timedelta(hours=2)))
    with pytest.raises(SessionError, match="--chain N") as e:
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=lambda m: None)
    assert "1: 20250301082921.WAV, 20250301082924.WAV" in str(e.value)
    assert "2: 20250301102921.WAV" in str(e.value)
    with pytest.raises(SessionError, match="вне диапазона"):
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), chain=3,
                         log=lambda m: None)


def test_pause_refused_in_folder(tmp_path, no_decode):
    make_folder(tmp_path / "in", chain_starts(2, gap=60))
    with pytest.raises(SessionError, match="склейка пауз"):
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=lambda m: None)


def test_riff_limit_refused_before_copy(tmp_path, no_decode, monkeypatch):
    make_folder(tmp_path / "in", chain_starts(2))
    monkeypatch.setattr(audio, "data_bytes_ok", lambda *a: False)
    with pytest.raises(SessionError, match="4 ГиБ"):
        assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=lambda m: None)
    assert not any((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def run_session(tmp_path, **kw):
    logs = []
    dst = assemble_session(str(tmp_path / "in"), str(tmp_path / "out"), log=logs.append, **kw)
    return dst, json.loads((dst / "session.json").read_text()), "\n".join(logs)


@pytest.mark.slow
def test_session_bit_exact(tmp_path):
    paths = make_folder(tmp_path / "in", chain_starts(3))
    before = [sha256_file(str(p)) for p in paths]
    dst, s, logs = run_session(tmp_path)
    assert dst.name == f"2025-03-01_08-29_0h00m.{s['session_id'].split('.')[1]}.session"
    assert s["session_id"] == dst.name.removesuffix(".session")
    got, _ = sf.read(dst / s["wav"], dtype="int16")
    want = np.concatenate([sf.read(p, dtype="int16")[0] for p in paths])
    assert np.array_equal(got, want) and sf.info(dst / s["wav"]).subtype == "PCM_16"
    assert [sha256_file(str(p)) for p in paths] == before == [f["sha256"] for f in s["files"]]
    assert s["session_sha256"] == sha256_file(str(dst / s["wav"]))
    assert s["n_samples"] == 3 * 2 * SR
    assert [f["offset_samples"] for f in s["files"]] == [0, 2 * SR, 4 * SR]
    assert [x["offset_samples"] for x in s["seams"]] == [2 * SR, 4 * SR]
    assert {x["kind"] for x in s["seams"]} == {"split"}
    assert s["seams"][0]["gap_est_s"] == pytest.approx(1.0)
    assert s["wallclock_source"] == "filename" and s["clock_offset_s"] == 0
    assert "--start" in logs and "ВНИМАНИЕ" not in logs.replace("ВНИМАНИЕ: каналы", "")
    run = json.loads((dst / "run.json").read_text())
    assert set(run["env"]) == set(audio.SESSION_ENV_KEYS) and "ffmpeg_version" in run
    assert not dst.with_name(dst.name + ".tmp").exists()


@pytest.mark.slow
def test_session_chain_2(tmp_path):
    make_folder(tmp_path / "in", chain_starts(1) + chain_starts(2, T0 + timedelta(hours=2)))
    _, s, _ = run_session(tmp_path, chain=2)
    assert [f["name"] for f in s["files"]] == ["20250301102921.WAV", "20250301102924.WAV"]
    assert s["name"].startswith("2025-03-01_10-29")


def _stereo(left, right):
    return np.stack([left, right], axis=1).astype(np.int16)


@pytest.mark.slow
@pytest.mark.parametrize("channels, data, expected", [
    (1, lambda i, n: np.arange(n, dtype=np.int16).reshape(-1, 1), None),
    (2, lambda i, n: _stereo(np.arange(n), np.arange(n)), True),
    (2, lambda i, n: _stereo(np.arange(n), np.r_[np.arange(n - 1), 7 if i == 1 else n - 1]),
     False),
    (3, lambda i, n: np.repeat(np.arange(n, dtype=np.int16).reshape(-1, 1), 3, axis=1), True),
])
def test_channels_identical(tmp_path, channels, data, expected):
    make_folder(tmp_path / "in", chain_starts(2), channels=channels, data=data)
    _, s, logs = run_session(tmp_path)
    assert s["channels_identical"] is expected
    if expected is False:
        assert "противофазе" in logs and "--channel mix" not in logs
    if expected is None:
        assert "канал" not in logs


@pytest.mark.slow
def test_session_reuse_and_env(tmp_path, monkeypatch):
    make_folder(tmp_path / "in", chain_starts(2))
    dst, _, _ = run_session(tmp_path)
    mtime = (dst / "session.json").stat().st_mtime_ns
    monkeypatch.setattr(audio, "sha256_file", lambda p: pytest.fail("повтор читает данные"))
    dst2, _, logs = run_session(tmp_path)
    assert dst2 == dst and (dst / "session.json").stat().st_mtime_ns == mtime
    assert "каналы различаются" in logs  # совет напечатан заново
    monkeypatch.undo()
    run = json.loads((dst / "run.json").read_text())
    (dst / "run.json").write_text(json.dumps(run | {"ffmpeg_version": "0.0"}))
    assert run_session(tmp_path)[0] == dst  # ffmpeg только для справки
    run["env"]["libsndfile_version"] = "0.0"
    (dst / "run.json").write_text(json.dumps(run))
    with pytest.raises(RunError, match="rm -rf"):
        run_session(tmp_path)


@pytest.mark.slow
def test_session_reuse_missing_wav(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2))
    dst, s, _ = run_session(tmp_path)
    (dst / s["wav"]).unlink()
    with pytest.raises(RunError, match="rm -rf"):
        run_session(tmp_path)


@pytest.mark.slow
def test_orphan_tmp_removed(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2))
    dst, _, _ = run_session(tmp_path)
    os.replace(dst, dst.with_name(dst.name + ".tmp"))
    (dst.with_name(dst.name + ".tmp") / "junk").write_text("x")
    dst2, _, _ = run_session(tmp_path)
    assert dst2 == dst and not (dst / "junk").exists()
    assert not dst.with_name(dst.name + ".tmp").exists()


@pytest.mark.slow
def test_session_start_fix(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2))
    plain, _, _ = run_session(tmp_path)
    dst, s, logs = run_session(tmp_path, start="2026-10-20 11:30:21")
    assert dst != plain and dst.name.startswith("2026-10-20_11-30_0h00m.")
    assert s["wallclock_source"] == "filename+start"
    assert s["files"][0]["recorder_time"] == "2025-03-01T08:29:21"
    assert s["files"][1]["start_local"] == "2026-10-20T11:30:24"
    assert s["clock_offset_s"] == (datetime(2026, 10, 20, 11, 30, 21) - T0).total_seconds()
    assert "повторите с --start" not in logs
    with pytest.raises(SessionError, match="2025-03-01 09:00"):
        run_session(tmp_path, start="20.10.2026 11:30")


@pytest.mark.slow
def test_mtime_mismatch_warns_and_assembles(tmp_path):
    paths = make_folder(tmp_path / "in", chain_starts(2))
    os.utime(paths[0], (paths[1].stat().st_mtime + 100,) * 2)
    dst, _, logs = run_session(tmp_path)
    assert "порядок файлов по mtime" in logs and dst.exists()


# --- probs / segment / export on a session (D25, D27) ---


CFG = parse_config("threshold = 0.5\npad_ms = 0\nmerge_gap_ms = 0\n")[0]


def same_channels(i, n):
    x = np.random.default_rng(i).integers(-20000, 20000, n, dtype=np.int16)
    return np.stack([x, x], axis=1)


@pytest.mark.slow
def test_session_probs_segment_export(tmp_path):
    make_folder(tmp_path / "in", chain_starts(3), data=same_channels)
    sdir, s, _ = run_session(tmp_path)
    pdir = compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"))
    assert pdir.name.endswith(".c0.raw")
    pj = json.loads((pdir / "probs.json").read_text())
    assert (pj["channel"], pj["channel_source"]) == (0, "session.channels_identical")
    assert pj["session"] == s and pj["seam_reset_frames"] == [62, 125]  # D29
    assert compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"), 0) == pdir  # тот же .c0
    seg = segment(str(pdir), CFG)
    seams = [json.loads(x) for x in (seg / "seams.jsonl").read_text().splitlines()]
    assert [x["offset_samples"] for x in seams] == [2 * SR, 4 * SR]
    assert all(x["session_id"] == s["session_id"] for x in seams)
    clips = export_clips(str(seg))
    whole, _ = sf.read(sdir / s["wav"], dtype="int16")
    for x in [json.loads(r) for r in (clips / "seams.jsonl").read_text().splitlines()]:
        got, _ = sf.read(seg / x["audio_path"], dtype="int16")
        assert np.array_equal(got, whole[x["start_sample"]:x["end_sample"]])
    rows = [json.loads(r) for r in (seg / "manifest.jsonl").read_text().splitlines()]
    # segment читает только probs.json: без каталога сессии строки те же
    shutil.rmtree(sdir)
    shutil.rmtree(seg)
    again = [json.loads(r) for r in
             (segment(str(pdir), CFG) / "manifest.jsonl").read_text().splitlines()]
    assert again == rows


@pytest.mark.slow
def test_session_lr_differ_needs_channel(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2))
    sdir, s, _ = run_session(tmp_path)
    with pytest.raises(SourceError, match="задайте channel"):
        compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"))


@pytest.mark.slow
def test_session_sha_mismatch_refused(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2), data=same_channels)
    sdir, s, _ = run_session(tmp_path)
    (sdir / "session.json").write_text(json.dumps(s | {"session_sha256": "0" * 64}))
    with pytest.raises(RunError, match="session_sha256"):
        compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"))
    assert not (tmp_path / "probs").exists() or not any((tmp_path / "probs").iterdir())


@pytest.mark.slow
def test_two_starts_same_minute_refused_on_reuse(tmp_path):
    make_folder(tmp_path / "in", chain_starts(2), data=same_channels)
    a, sa, _ = run_session(tmp_path, start="2026-10-20 11:30:00")
    b, sb, _ = run_session(tmp_path, start="2026-10-20 11:30:30")
    assert a != b and sa["name"] == sb["name"]
    compute_probs(str(a / sa["wav"]), str(tmp_path / "probs"))
    with pytest.raises(RunError, match="(?s)блок session.*rm -rf"):
        compute_probs(str(b / sb["wav"]), str(tmp_path / "probs"))


@pytest.mark.slow
def test_probs_without_seam_reset_refused_on_reuse(tmp_path):
    """D29: a session probs dir from before the seam reset is not reused silently."""
    make_folder(tmp_path / "in", chain_starts(2), data=same_channels)
    sdir, s, _ = run_session(tmp_path)
    pdir = compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"))
    pj = json.loads((pdir / "probs.json").read_text())
    del pj["seam_reset_frames"]
    (pdir / "probs.json").write_text(json.dumps(pj))
    with pytest.raises(RunError, match="(?s)сброс.*rm -rf"):
        compute_probs(str(sdir / s["wav"]), str(tmp_path / "probs"))


# --- cli ---



def test_cli_session_refusal_exit_2(tmp_path, capsys, no_decode):
    make_folder(tmp_path / "in", chain_starts(1) + chain_starts(1, T0 + timedelta(hours=2)))
    assert main(["session", str(tmp_path / "in"), "--out", str(tmp_path / "out")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("voxcut: ") and "--chain N" in err and "Traceback" not in err
    assert main(["session", str(tmp_path / "in"), "--out", str(tmp_path / "out"),
                 "--start", "вчера"]) == 2
    assert "2025-03-01 09:00" in capsys.readouterr().err
    assert main(["session", str(tmp_path / "nope"), "--out", str(tmp_path / "out")]) == 2


@pytest.mark.slow
def test_cli_session_prints_dir(tmp_path, capsys):
    make_folder(tmp_path / "in", chain_starts(1) + chain_starts(2, T0 + timedelta(hours=2)))
    assert main(["session", str(tmp_path / "in"), "--out", str(tmp_path / "out"),
                 "--chain", "2", "--start", "2026-10-20 11:30"]) == 0
    out, err = capsys.readouterr()
    dst = tmp_path / "out" / out.strip().split("/")[-1]
    assert dst.name.startswith("2026-10-20_11-30_0h00m.") and (dst / "session.json").exists()
    assert "инвентарь" in err
