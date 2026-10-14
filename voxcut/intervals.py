"""Pure interval logic: no torch or silero imports here (D1)."""

import difflib
import hashlib
import json
import math
import tomllib
from dataclasses import dataclass, field, fields

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


# --- labels (D7) ---

LABELS = ("scored", "me", "other_speech", "vocals_music")


class LabelsError(ValueError):
    pass


def union(segs) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(segs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def intersect(a, b) -> list[tuple[float, float]]:
    """Intersection of two unions (sorted, non-overlapping)."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract(a, b) -> list[tuple[float, float]]:
    """a minus b, both unions."""
    out = []
    for s, e in a:
        for bs, be in b:
            if be <= s or bs >= e:
                continue
            if bs > s:
                out.append((s, bs))
            s = max(s, be)
            if s >= e:
                break
        if s < e:
            out.append((s, e))
    return out


def total(segs) -> float:
    return sum(e - s for s, e in segs)


def parse_labels(text: str, duration_s: float | None = None) -> dict[str, list]:
    """Audacity labels (TSV start, end, label in seconds) -> {layer: union}; all errors at once."""
    layers: dict[str, list] = {k: [] for k in LABELS}
    lines: dict[str, list] = {k: [] for k in LABELS}
    errors = []
    for no, line in enumerate(text.splitlines(), 1):
        if line.startswith("\\") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            errors.append(f"строка {no}: ожидается 3 поля через TAB, получено {len(fields)}")
            continue
        try:
            s, e = float(fields[0]), float(fields[1])
        except ValueError:
            s = e = math.nan
        if not (math.isfinite(s) and math.isfinite(e)):  # float() also accepts nan and inf
            errors.append(f"строка {no}: start/end не числа: {fields[0]!r}, {fields[1]!r}")
            continue
        label = fields[2].strip().lower()
        if label not in LABELS:
            errors.append(f"строка {no}: неизвестная метка {fields[2]!r}, допустимы: "
                          f"{', '.join(LABELS)}")
            continue
        if s >= e:
            errors.append(f"строка {no}: start {s} ≥ end {e}")
            continue
        if duration_s is not None and e > duration_s:
            errors.append(f"строка {no}: end {e} больше длительности источника {duration_s}")
        layers[label].append((s, e))
        lines[label].append((no, s, e))
    scored = union(layers["scored"])
    for label in LABELS[1:]:
        for no, s, e in lines[label]:
            if total(intersect([(s, e)], scored)) < e - s:
                errors.append(f"строка {no}: метка {label} [{s}, {e}] выходит за scored")
    if errors:
        raise LabelsError("ошибки в разметке:\n" + "\n".join(errors))
    return {k: union(v) for k, v in layers.items()}


# --- config (D10) ---

class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SegmentConfig:
    threshold: float
    pad_ms: int
    merge_gap_ms: int
    neg_threshold: float | None = None
    min_speech_ms: int = 0
    min_silence_ms: int = 100
    channel: str | int | None = None  # probs parameter (D21): keyed by the probs dir, not here

    def effective(self) -> dict:
        """Segmentation parameters as written to the manifest (neg_threshold resolved)."""
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "channel"}
        if d["neg_threshold"] is None:
            d["neg_threshold"] = default_neg_threshold(self.threshold)
        return d

    def sha256(self) -> str:
        blob = json.dumps(self.effective(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


def _check(key: str, value, kind: str):
    if isinstance(value, bool):
        return f"{key}: ожидается {kind}, получено {value!r}"
    if kind == "prob" and not (isinstance(value, int | float) and 0 < value <= 1):
        return f"{key}: ожидается число в (0, 1], получено {value!r}"
    if kind == "ms" and not (isinstance(value, int) and value >= 0):
        return f"{key}: ожидается целое ≥ 0 (мс), получено {value!r}"
    if kind == "channel" and not (value in ("mono", "mix") or
                                  (isinstance(value, int) and value >= 0)):
        return f'{key}: ожидается "mono", "mix" или индекс ≥ 0, получено {value!r}'
    return None


_KINDS = {"threshold": "prob", "neg_threshold": "prob", "pad_ms": "ms", "merge_gap_ms": "ms",
          "min_speech_ms": "ms", "min_silence_ms": "ms", "channel": "channel"}


def parse_config(text: str) -> tuple[SegmentConfig, dict]:
    """TOML -> (SegmentConfig, meta). Unknown key, wrong type or range -> ConfigError."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"конфиг не TOML: {e}") from e
    meta = raw.pop("meta", {})
    errors = []
    for key, value in raw.items():
        if key not in _KINDS:
            hint = difflib.get_close_matches(key, _KINDS, n=1)
            errors.append(f"неизвестный ключ {key!r}"
                          + (f", возможно {hint[0]!r}" if hint else "")
                          + f"; допустимы: {', '.join(_KINDS)}, [meta]")
        elif msg := _check(key, value, _KINDS[key]):
            errors.append(msg)
    missing = [k for k in ("threshold", "pad_ms", "merge_gap_ms") if k not in raw]
    errors += [f"нет обязательного ключа {k!r}" for k in missing]
    if not errors and raw.get("neg_threshold", 0) > raw["threshold"]:
        errors.append(f"neg_threshold {raw['neg_threshold']} больше threshold {raw['threshold']}")
    if errors:
        raise ConfigError("ошибки в конфиге:\n" + "\n".join(errors))
    for key in ("threshold", "neg_threshold"):
        if key in raw:
            raw[key] = float(raw[key])
    return SegmentConfig(**raw), meta


# --- metrics (D8, D20) ---

TOLERANCE_S = 0.1  # D20: fixed collar around `me`
EVENT_COV = 0.5  # D8 default
TRUNCATED_BELOW = 0.95


@dataclass
class Metrics:
    """Sums, so ALL = sum of sessions (D8: aggregates are sum/sum)."""
    me_sec: float = 0.0
    me_covered_sec: float = 0.0
    raw_covered_sec: float = 0.0
    events: int = 0
    events_covered: int = 0
    fully_missed: list = field(default_factory=list)
    truncated: list = field(default_factory=list)
    fa_asr_sec: float = 0.0
    fa_asr_den: float = 0.0
    fa_bound_sec: float = 0.0
    fa_bound_den: float = 0.0
    neutral_sec: float = 0.0
    clip_sec: float = 0.0
    n_clips: int = 0
    scored_sec: float = 0.0

    def __add__(self, other: "Metrics") -> "Metrics":
        return Metrics(**{f.name: getattr(self, f.name) + getattr(other, f.name)
                          for f in fields(self)})

    @staticmethod
    def _ratio(num, den, scale=1.0):
        return num / den * scale if den > 0 else math.nan

    @property
    def recall_sec(self):
        return self._ratio(self.me_covered_sec, self.me_sec)

    @property
    def raw_recall_sec(self):
        return self._ratio(self.raw_covered_sec, self.me_sec)

    @property
    def recall_events(self):
        return self._ratio(self.events_covered, self.events)

    @property
    def fa_asr_min_h(self):
        return self._ratio(self.fa_asr_sec, self.fa_asr_den, 60)

    @property
    def fa_bound_min_h(self):
        return self._ratio(self.fa_bound_sec, self.fa_bound_den, 60)

    @property
    def neutral_min_h(self):
        return self._ratio(self.neutral_sec, self.scored_sec, 60)

    @property
    def clip_min_h(self):
        return self._ratio(self.clip_sec, self.scored_sec, 60)

    @property
    def clips_per_h(self):
        return self._ratio(self.n_clips, self.scored_sec, 3600)

    @property
    def coverage(self):
        return self._ratio(self.clip_sec, self.scored_sec)

    @property
    def whole_file(self) -> bool:
        return self.coverage > 0.9  # D20(d): "≈весь файл"


def evaluate_session(final_s, labels: dict, raw_s=None, event_cov: float = EVENT_COV,
                     tolerance_s: float = TOLERANCE_S) -> Metrics:
    """Metrics for one session; every interval is first cut to `scored` (C5). Seconds."""
    scored = labels["scored"]
    me = labels["me"]
    det = intersect(union(final_s), scored)
    unlabeled = subtract(subtract(subtract(scored, me), labels["other_speech"]),
                         labels["vocals_music"])
    collar = union((s - tolerance_s, e + tolerance_s) for s, e in me)
    unlabeled_bound = subtract(unlabeled, collar)
    m = Metrics(
        me_sec=total(me),
        me_covered_sec=total(intersect(det, me)),
        events=len(me),
        fa_asr_sec=total(intersect(det, unlabeled)),
        fa_asr_den=total(unlabeled),
        fa_bound_sec=total(intersect(det, unlabeled_bound)),
        fa_bound_den=total(unlabeled_bound),
        neutral_sec=total(intersect(det, union(labels["other_speech"] + labels["vocals_music"]))),
        clip_sec=total(det),
        n_clips=sum(1 for seg in union(final_s) if intersect([seg], scored)),
        scored_sec=total(scored),
    )
    if raw_s is not None:
        m.raw_covered_sec = total(intersect(intersect(union(raw_s), scored), me))
    for s, e in me:
        cov = total(intersect(det, [(s, e)])) / (e - s)
        m.events_covered += cov >= event_cov
        if cov == 0:
            m.fully_missed.append((s, e))
        elif cov < TRUNCATED_BELOW:
            m.truncated.append((s, e, cov))
    return m
