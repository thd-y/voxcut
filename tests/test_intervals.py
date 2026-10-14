import math
import subprocess
import sys

import numpy as np
import pytest

from voxcut.intervals import FRAME_SAMPLES as F
from voxcut.intervals import (
    ConfigError,
    LabelsError,
    Metrics,
    default_neg_threshold,
    evaluate_session,
    parse_config,
    parse_labels,
    postprocess,
    segment_raw,
)

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


# --- T5: labels (D7) ---

def lab(*rows):
    return "\n".join("\t".join(map(str, r)) for r in rows) + "\n"


def test_labels_ok_normalizes_skips_and_merges():
    text = lab((0, 100, "scored"), (10, 12, "Me "), (11, 13, "me"), (20, 21, "OTHER_SPEECH")) \
        + "\\\t1000.0\t2000.0\n"  # строка спектрального выделения Audacity
    got = parse_labels(text)
    assert got["me"] == [(10, 13)] and got["other_speech"] == [(20, 21)]
    assert got["scored"] == [(0, 100)] and got["vocals_music"] == []


@pytest.mark.parametrize("text, needle", [
    (lab((0, 10, "scored"), (1, 2, "noise")), "неизвестная метка 'noise'"),
    (lab((0, 10, "scored")) + "1 2 me\n", "3 поля"),
    (lab((0, 10, "scored"), (2, 2, "me")), "start 2.0 ≥ end 2.0"),
    (lab((0, 10, "scored"), ("x", 2, "me")), "не числа"),
    (lab((0, 10, "scored"), (9, 11, "me")), "выходит за scored"),
    (lab((0, 10, "scored"), (5, 6, "me"), (20, 30, "scored")), "длительности источника"),
])
def test_labels_errors(text, needle):
    with pytest.raises(LabelsError, match=needle):
        parse_labels(text, duration_s=25)


def test_labels_all_errors_in_one_list():
    text = lab((0, 10, "scored"), (1, 2, "noise"), (3, 3, "me"), (9, 12, "me"))
    with pytest.raises(LabelsError) as e:
        parse_labels(text)
    assert [ln.split(":")[0] for ln in str(e.value).splitlines()[1:]] == \
        ["строка 2", "строка 3", "строка 4"]
    assert "scored, me, other_speech, vocals_music" in str(e.value)


# --- T5: config (D10) ---

BASE = "threshold = 0.35\npad_ms = 250\nmerge_gap_ms = 300\n"


def test_config_defaults_and_meta():
    cfg, meta = parse_config(BASE + '[meta]\nname = "a"\nnote = "x"\n')
    assert meta == {"name": "a", "note": "x"}
    assert cfg.effective() == {"threshold": 0.35, "neg_threshold": pytest.approx(0.2),
                               "pad_ms": 250, "merge_gap_ms": 300, "min_speech_ms": 0,
                               "min_silence_ms": 100}


@pytest.mark.parametrize("text, needle", [
    (BASE + "treshold = 0.2\n", "неизвестный ключ 'treshold', возможно 'threshold'"),
    ("threshold = 1.5\npad_ms = 250\nmerge_gap_ms = 300\n", r"threshold: ожидается число в \(0, 1\]"),
    ("threshold = 0.5\npad_ms = 250.5\nmerge_gap_ms = 300\n", "pad_ms: ожидается целое"),
    ("threshold = 0.5\npad_ms = -1\nmerge_gap_ms = 300\n", "pad_ms: ожидается целое"),
    ("threshold = true\npad_ms = 1\nmerge_gap_ms = 300\n", "threshold"),
    ("threshold = 0.5\npad_ms = 250\n", "нет обязательного ключа 'merge_gap_ms'"),
    (BASE + "neg_threshold = 0.5\n", "neg_threshold 0.5 больше threshold"),
    (BASE + 'channel = "left"\n', "channel"),
    ("threshold = \n", "не TOML"),
])
def test_config_errors(text, needle):
    with pytest.raises(ConfigError, match=needle):
        parse_config(text)


def test_config_sha_ignores_key_order_meta_and_channel():
    a, _ = parse_config(BASE)
    b, _ = parse_config('merge_gap_ms = 300\npad_ms = 250\nthreshold = 0.35\nchannel = 0\n'
                        '[meta]\nname = "b"\n')
    c, _ = parse_config(BASE.replace("0.35", "0.36"))
    assert a.sha256() == b.sha256() != c.sha256()


# --- T5: metrics (D8, D20) ---

def L(scored, me=(), other=(), vocals=()):
    return {"scored": list(scored), "me": list(me), "other_speech": list(other),
            "vocals_music": list(vocals)}


def test_metrics_formulas():
    # scored 0–100 с; me 10–20 и 50–52; чужая речь 70–80
    labels = L([(0, 100)], me=[(10, 20), (50, 52)], other=[(70, 80)])
    final = [(9.95, 15), (60, 61), (75, 76), (150, 160)]  # последний вне scored (C5)
    m = evaluate_session(final, labels, raw_s=[(10, 12)])
    assert m.recall_sec == pytest.approx(5 / 12)
    assert m.raw_recall_sec == pytest.approx(2 / 12)
    assert m.recall_events == pytest.approx(0.5)  # 10–20 покрыто ровно на 0.5, 50–52 — 0
    assert m.fully_missed == [(50, 52)] and m.truncated == [(10, 20, 0.5)]
    unl = 100 - 12 - 10
    assert m.fa_asr_min_h == pytest.approx((0.05 + 1) / unl * 60)
    # воротник ±0.1 с вокруг me: 9.95 попадает в воротник, FA_bound его не считает
    assert m.fa_bound_min_h == pytest.approx(1 / (unl - 0.4) * 60)
    assert m.neutral_min_h == pytest.approx(1 / 100 * 60)
    assert m.clip_min_h == pytest.approx((5.05 + 1 + 1) / 100 * 60)
    assert m.clips_per_h == pytest.approx(3 / 100 * 3600)
    assert m.coverage == pytest.approx(7.05 / 100) and not m.whole_file


@pytest.mark.parametrize("cov_end, covered", [(10 + 4.9, 0), (10 + 5.0, 1)])
def test_event_cov_boundary(cov_end, covered):
    m = evaluate_session([(10, cov_end)], L([(0, 100)], me=[(10, 20)]))
    assert m.events_covered == covered


def test_na_and_all_is_sum_over_sum():
    silent = evaluate_session([(1, 2)], L([(0, 3600)]))
    speech = evaluate_session([(10, 15)], L([(0, 100)], me=[(10, 20)]))
    assert math.isnan(silent.recall_sec) and math.isnan(silent.recall_events)
    assert math.isnan(Metrics().fa_asr_min_h)
    total = silent + speech
    assert total.recall_sec == speech.recall_sec == 0.5
    assert total.fa_asr_min_h == pytest.approx(1 / (3600 + 90) * 60)
    assert total.fully_missed == [] and total.events == 1


def test_whole_file_flag():
    assert evaluate_session([(0, 95)], L([(0, 100)])).whole_file
