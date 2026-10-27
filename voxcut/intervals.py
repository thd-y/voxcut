"""Pure interval logic: no torch or silero imports here (D1)."""

import bisect
import difflib
import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta

from voxcut.errors import Coded

FRAME_SAMPLES = 512  # Silero frame at 16 kHz, 32 ms
VAD_SR = 16000
RESET_PERIOD_FRAMES = 3750  # D31: second Silero pass resets every 120 s at 16 kHz, frame 512


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
            errors.append(f"line {no}: expected 3 TAB-separated fields, got {len(fields)}")
            continue
        try:
            s, e = float(fields[0]), float(fields[1])
        except ValueError:
            s = e = math.nan
        if not (math.isfinite(s) and math.isfinite(e)):  # float() also accepts nan and inf
            errors.append(f"line {no}: start/end are not numbers: {fields[0]!r}, {fields[1]!r}")
            continue
        label = fields[2].strip().lower()
        if label not in LABELS:
            errors.append(f"line {no}: unknown label {fields[2]!r}, allowed: "
                          f"{', '.join(LABELS)}")
            continue
        if s >= e:
            errors.append(f"line {no}: start {s} ≥ end {e}")
            continue
        if duration_s is not None and e > duration_s:
            errors.append(f"line {no}: end {e} is past the source duration {duration_s}")
        layers[label].append((s, e))
        lines[label].append((no, s, e))
    scored = union(layers["scored"])
    for label in LABELS[1:]:
        for no, s, e in lines[label]:
            if total(intersect([(s, e)], scored)) < e - s:
                errors.append(f"line {no}: label {label} [{s}, {e}] goes outside scored")
    if errors:
        raise LabelsError("errors in the labels:\n" + "\n".join(errors))
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
        return f"{key}: expected {kind}, got {value!r}"
    if kind == "prob" and not (isinstance(value, int | float) and 0 < value <= 1):
        return f"{key}: expected a number in (0, 1], got {value!r}"
    if kind == "ms" and not (isinstance(value, int) and value >= 0):
        return f"{key}: expected an integer ≥ 0 (ms), got {value!r}"
    if kind == "channel" and not (value in ("mono", "mix") or
                                  (isinstance(value, int) and value >= 0)):
        return f'{key}: expected "mono", "mix" or an index ≥ 0, got {value!r}'
    return None


_KINDS = {"threshold": "prob", "neg_threshold": "prob", "pad_ms": "ms", "merge_gap_ms": "ms",
          "min_speech_ms": "ms", "min_silence_ms": "ms", "channel": "channel"}


def parse_config(text: str) -> tuple[SegmentConfig, dict]:
    """TOML -> (SegmentConfig, meta). Unknown key, wrong type or range -> ConfigError."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config is not TOML: {e}") from e
    meta = raw.pop("meta", {})
    errors = [] if isinstance(meta, dict) else [f"[meta]: expected a table, got {meta!r}"]
    for key, value in raw.items():
        if key not in _KINDS:
            hint = difflib.get_close_matches(key, _KINDS, n=1)
            errors.append(f"unknown key {key!r}"
                          + (f", did you mean {hint[0]!r}" if hint else "")
                          + f"; allowed: {', '.join(_KINDS)}, [meta]")
        elif msg := _check(key, value, _KINDS[key]):
            errors.append(msg)
    missing = [k for k in ("threshold", "pad_ms", "merge_gap_ms") if k not in raw]
    errors += [f"missing required key {k!r}" for k in missing]
    if not errors and raw.get("neg_threshold", 0) > raw["threshold"]:
        errors.append(f"neg_threshold {raw['neg_threshold']} is greater than threshold {raw['threshold']}")
    if errors:
        raise ConfigError("errors in the config:\n" + "\n".join(errors))
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
    session_clip_sec: float = 0.0  # D38: all clips, not cut to scored
    session_sec: float = 0.0

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
    def clip_min_h_session(self):
        return self._ratio(self.session_clip_sec, self.session_sec, 60)

    @property
    def clip_per_me(self):
        return self._ratio(self.session_clip_sec, self.me_sec) if self.session_sec > 0 else math.nan

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
                     tolerance_s: float = TOLERANCE_S, duration_s: float | None = None) -> Metrics:
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
    if duration_s is not None:
        m.session_clip_sec, m.session_sec = total(union(final_s)), duration_s
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


# --- session (D25–D27) ---

GAP_MIN_S = -2.0  # D26: covers the 1 s name step, not a real overlap
GAP_MAX_S = 5.0
PAUSE_MAX_S = 1800.0  # a longer gap starts a separate chain; a shorter one is refused for now
SEAM_TOL_S = 1.0
MTIME_TOL_S = 5.0  # D25 (X7): mtime − name start vs duration
RIFF_MAX = 0xFFFFFFFF
WAV_HEADER = 44
START_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


class SessionError(Coded, ValueError):
    pass


def file_start(name: str) -> datetime | None:
    """The only place a file name is parsed. Recorder: YYYYMMDDHHMMSS.wav = start time."""
    m = re.fullmatch(r"(\d{14})\.wav", name, re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m[1], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_start(text: str) -> datetime:
    for f in START_FORMATS:
        try:
            return datetime.strptime(text, f)
        except ValueError:
            pass
    raise SessionError("session_bad_start", text=repr(text))


def session_name(start: datetime, n_samples: int, sr: int) -> str:
    """<YYYY-MM-DD>_<HH-MM>_<H>h<MM>m; minutes rounded half up, not to even (D25)."""
    minutes = math.floor(n_samples / sr / 60 + 0.5)
    return f"{start:%Y-%m-%d_%H-%M}_{minutes // 60}h{minutes % 60:02d}m"


def gap_s(prev: tuple[str, datetime, int], cur: tuple[str, datetime, int], sr: int) -> float:
    """Seconds between the end of `prev` and the start of `cur`, parts as (name, start, n)."""
    return (cur[1] - prev[1]).total_seconds() - prev[2] / sr


def split_chains(parts: list[tuple[str, datetime, int]], sr: int) -> list[list[int]]:
    """(name, start, n_samples) -> chains of indices in start order. Names are only for messages."""
    order = sorted(range(len(parts)), key=lambda i: parts[i][1])
    chains: list[list[int]] = []
    for i in order:
        if chains:
            prev = parts[chains[-1][-1]]
            gap = gap_s(prev, parts[i], sr)
            if gap < GAP_MIN_S:
                raise SessionError("session_overlap", a=prev[0], b=parts[i][0], gap=f"{-gap:.2f}",
                                   tol=f"{-GAP_MIN_S:g}")
            if GAP_MAX_S < gap <= PAUSE_MAX_S:
                raise SessionError("session_pause", minutes=f"{gap / 60:.1f}", a=prev[0],
                                   b=parts[i][0])
            if gap <= GAP_MAX_S:
                chains[-1].append(i)
                continue
        chains.append([i])
    return chains


def pick_chain(chains: list[list[int]], n: int | None, names: list[str]) -> list[int]:
    """One chain, or the N-th (1-based) by --chain; otherwise refuse with a numbered list."""
    lines = [f"  {k}: {', '.join(names[i] for i in c)}" for k, c in enumerate(chains, 1)]
    if n is None:
        if len(chains) == 1:
            return chains[0]
        raise SessionError("session_chain_choose", n=len(chains), lines=lines)
    if not 1 <= n <= len(chains):
        raise SessionError("session_chain_range", n=n, total=len(chains), lines=lines)
    return chains[n - 1]


def meta_key(files: list[tuple[str, int, int]], start: str | None) -> str:
    """sha8 of ordered (name, size_bytes, frames) + --start: found again without reading data."""
    blob = json.dumps({"files": [list(f) for f in files], "start": start}).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def data_bytes_ok(n_samples: int, channels: int, bytes_per_sample: int) -> bool:
    return n_samples * channels * bytes_per_sample + WAV_HEADER <= RIFF_MAX


def locate(sample: int, offsets: list[int]) -> tuple[int, int]:
    """Session sample -> (file index, sample within that file); offsets start at 0."""
    i = bisect.bisect_right(offsets, sample) - 1
    return i, sample - offsets[i]


def start_local(file_start_time: datetime, sample: int, sr: int, clock_offset_s: float) -> str:
    """Local time without a zone, ±1 s (name step)."""
    t = file_start_time + timedelta(seconds=clock_offset_s + sample / sr)
    return t.isoformat(timespec="seconds")


def at_seam(start: int, end: int, seams: list[int], tol: int) -> bool:
    """[start, end) overlaps [seam − tol, seam + tol) of any seam."""
    return any(start < s + tol and end > s - tol for s in seams)


def seam_windows(seams: list[int], tol: int, n_samples: int) -> list[tuple[int, int]]:
    return [(max(0, s - tol), min(n_samples, s + tol)) for s in seams]


def seam_frames(seams: list[int], sr: int) -> list[int]:
    """Frames before which Silero restarts (D29): the frame holding each seam; frame 0 already does."""
    return [f for f in (s * VAD_SR // (sr * FRAME_SAMPLES) for s in seams) if f > 0]


def mtime_warnings(parts: list[tuple[str, datetime, int]], mtime_ends: list[datetime],
                   sr: int) -> list[str]:
    """Warnings, not refusals (D25, X7): a clock reset backwards hides behind name order."""
    out = []
    by_name = sorted(range(len(parts)), key=lambda i: parts[i][1])
    if by_name != sorted(range(len(parts)), key=lambda i: mtime_ends[i]):
        out.append("file order by mtime differs from order by name: "
                   "the recorder clock may have been reset")
    for (name, start, n), end in zip(parts, mtime_ends, strict=True):
        drift = (end - start).total_seconds() - n / sr
        if abs(drift) > MTIME_TOL_S:
            out.append(f"{name}: mtime minus the time in the name differs from the duration "
                       f"by {drift:+.1f} s")
    return out
