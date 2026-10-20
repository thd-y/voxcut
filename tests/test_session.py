from datetime import datetime, timedelta

import pytest

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
    parse_start,
    pick_chain,
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
