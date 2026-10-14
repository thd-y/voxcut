import subprocess
import sys

import numpy as np
import pytest

from voxcut.intervals import FRAME_SAMPLES as F
from voxcut.intervals import default_neg_threshold, postprocess, segment_raw

S, Q = 0.9, 0.1  # речь, тишина

# (probs, n16k, threshold, neg_threshold, min_speech_ms, min_silence_ms, expected 16 kHz intervals)
CASES = {
    "empty": ([], 0, 0.5, None, 0, 100, []),
    "silence": ([Q] * 10, 10 * F, 0.5, None, 0, 100, []),
    # ровно threshold открывает; ровно neg_threshold (0.35) не закрывает, 0.34 закрывает
    "exact_thresholds": ([0.5, 0.5, 0.35, 0.34] + [Q] * 5, 9 * F, 0.5, 0.35, 0, 100,
                         [(0, 3 * F)]),
    "pause_shorter_than_min_silence": ([S, S, Q, Q, Q, S, S] + [Q] * 6, 13 * F, 0.5, None, 0, 100,
                                       [(0, 7 * F)]),
    "two_segments": ([S, S] + [Q] * 5 + [S, S] + [Q] * 5, 14 * F, 0.5, None, 0, 100,
                     [(0, 2 * F), (7 * F, 9 * F)]),
    "open_at_eof": ([Q, Q, S, S, S], 5 * F - 100, 0.5, None, 0, 100, [(2 * F, 5 * F - 100)]),
    "min_speech_drops_short": ([S] + [Q] * 5 + [S, S] + [Q] * 5, 13 * F, 0.5, None, 40, 100,
                               [(6 * F, 8 * F)]),
    # сегмент ровно min_speech (64 мс = 2 кадра) отбрасывается: у апстрима строго >
    "min_speech_exact_dropped": ([S, S] + [Q] * 5 + [S, S, S] + [Q] * 5, 15 * F, 0.5, None, 64,
                                 100, [(7 * F, 10 * F)]),
    "neg_threshold_band_keeps_speech": ([S, 0.4, 0.4, 0.4, 0.4, 0.4] + [Q] * 5, 11 * F, 0.5, None,
                                        0, 100, [(0, 6 * F)]),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_segment_raw_table(case):
    probs, n16k, th, neg, min_speech, min_sil, expected = case
    assert segment_raw(probs, th, neg, min_speech, min_sil, n16k) == expected


def test_table_has_multi_segment_case():
    assert any(len(c[-1]) >= 2 for c in CASES.values())  # D22a


def upstream(probs, n16k, th, neg, min_speech, min_sil):
    from silero_vad.utils_vad import get_speech_timestamps_from_probs

    out = get_speech_timestamps_from_probs(
        list(probs), sampling_rate=16000, threshold=th, neg_threshold=neg,
        min_speech_duration_ms=min_speech, min_silence_duration_ms=min_sil, speech_pad_ms=0,
        max_speech_duration_s=float("inf"), audio_length_samples=n16k)
    return [(d["start"], d["end"]) for d in out]


@pytest.mark.slow
@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_table_matches_upstream(case):
    *args, expected = case
    assert upstream(*args) == expected


@pytest.mark.slow
def test_random_probs_match_upstream():
    rng = np.random.default_rng(1)
    for _ in range(300):
        n = int(rng.integers(0, 400))
        # гладкие вероятности с долгими участками, как у настоящего VAD
        probs = np.clip(np.cumsum(rng.normal(0, 0.15, n)) % 2 - 0.5, 0, 1).astype(np.float32)
        th = float(rng.choice([0.1, 0.3, 0.5, 0.7]))
        neg = None if rng.random() < 0.5 else float(rng.uniform(0.01, th))
        p, n16k = probs.tolist(), n * F - int(rng.integers(0, F)) if n else 0
        ms, msil = int(rng.choice([0, 50, 250])), int(rng.choice([0, 100, 500, 2000]))
        assert segment_raw(p, th, neg, ms, msil, n16k) == upstream(p, n16k, th, neg, ms, msil)


def test_default_neg_threshold():
    assert default_neg_threshold(0.5) == pytest.approx(0.35)
    assert default_neg_threshold(0.1) == 0.01


# G1: края цепочки merge -> pad -> re-merge -> floor/ceil -> clip
@pytest.mark.parametrize("raw, merge_gap_ms, pad_ms, sr, n_src, expected", [
    ([(0, 1600), (3200, 4800)], 100, 0, 16000, 10**6, [(0, 1600), (3200, 4800)]),  # разрыв = gap
    ([(0, 1600), (3184, 4800)], 100, 0, 16000, 10**6, [(0, 4800)]),  # разрыв < gap
    ([(800, 1600)], 0, 100, 16000, 10**6, [(0, 3200)]),  # паддинг у 0
    ([(800, 1600)], 0, 100, 16000, 2000, [(0, 2000)]),  # паддинг у source_n_samples
    ([(1600, 3200), (4000, 5600)], 0, 30, 16000, 10**6, [(1120, 6080)]),  # пересечение после pad
    ([(1600, 3200), (4160, 5600)], 0, 30, 16000, 10**6, [(1120, 6080)]),  # стык после pad
    ([(1600, 3200), (4161, 5600)], 0, 30, 16000, 10**6, [(1120, 3680), (4161 - 480, 6080)]),
    ([(1, 3)], 0, 0, 44100, 10**6, [(2, 9)]),  # floor(2.756), ceil(8.27)
    ([(15999, 16000)], 0, 0, 48000, 10**6, [(47997, 48000)]),
    ([], 100, 100, 16000, 10, []),
])
def test_postprocess_edges(raw, merge_gap_ms, pad_ms, sr, n_src, expected):
    assert postprocess(raw, merge_gap_ms, pad_ms, sr, n_src) == expected


def test_intervals_imports_no_torch():
    code = ("import sys, voxcut.intervals; "
            "assert not {'torch', 'silero_vad'} & set(sys.modules), sys.modules.keys()")
    subprocess.run([sys.executable, "-c", code], check=True)
