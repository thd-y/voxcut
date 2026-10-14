"""Pure interval logic: no torch or silero imports here (D1)."""

FRAME_SAMPLES = 512  # Silero frame at 16 kHz, 32 ms
VAD_SR = 16000


def n_frames(n16k: int) -> int:
    """Frames in the 16 kHz copy; the last partial frame is zero-padded (D6)."""
    return -(-n16k // FRAME_SAMPLES)


def default_neg_threshold(threshold: float) -> float:
    """Upstream default (D3): max(threshold - 0.15, 0.01)."""
    return max(threshold - 0.15, 0.01)


def segment_raw(probs, threshold: float, neg_threshold: float | None, min_speech_ms: float,
                min_silence_ms: float, n16k: int) -> list[tuple[int, int]]:
    """Hysteresis state machine, 16 kHz sample coordinates [start, end).

    Ported from silero-vad 6.2.3, silero_vad/utils_vad.py:421-588,
    get_speech_timestamps_from_probs, without the max_speech branch and with speech_pad_ms=0.
    Copyright (c) 2020-present Silero Team, MIT License.
    """
    if neg_threshold is None:
        neg_threshold = default_neg_threshold(threshold)
    min_speech_samples = VAD_SR * min_speech_ms / 1000
    min_silence_samples = VAD_SR * min_silence_ms / 1000
    triggered = False
    speeches = []
    start = temp_end = 0
    for i, p in enumerate(probs):
        cur = FRAME_SAMPLES * i
        if p >= threshold and temp_end:
            temp_end = 0
        if p >= threshold and not triggered:
            triggered, start = True, cur
            continue
        if p < neg_threshold and triggered:
            if not temp_end:
                temp_end = cur
            if cur - temp_end < min_silence_samples:
                continue
            if temp_end - start > min_speech_samples:
                speeches.append((start, temp_end))
            triggered, temp_end = False, 0
    if triggered and n16k - start > min_speech_samples:
        speeches.append((start, n16k))
    return speeches


def _merge(segs: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    """Join neighbours whose gap is < max_gap (max_gap=1 joins overlaps and touching ends)."""
    out: list[tuple[int, int]] = []
    for s, e in segs:
        if out and s - out[-1][1] < max_gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def postprocess(raw: list[tuple[int, int]], merge_gap_ms: int, pad_ms: int, sr: int,
                source_n_samples: int) -> list[tuple[int, int]]:
    """merge raw gaps < merge_gap -> pad -> re-merge overlaps -> floor/ceil to sr -> clip."""
    per_ms = VAD_SR // 1000
    segs = _merge(raw, merge_gap_ms * per_ms)
    pad = pad_ms * per_ms
    segs = _merge([(s - pad, e + pad) for s, e in segs], 1)
    out = []
    for s, e in segs:
        s_src = max(0, s * sr // VAD_SR)
        e_src = min(source_n_samples, -(-e * sr // VAD_SR))
        if s_src < e_src:
            out.append((s_src, e_src))
    return out
