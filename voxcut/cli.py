"""voxcut commands: probs, segment, detect, evaluate, sweep (D19, D24)."""

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

from voxcut.audio import (
    ENV_KEYS,
    FFmpegError,
    RunError,
    SourceError,
    check_env,
    clean_orphans,
    compute_probs,
    load_json,
)
from voxcut.intervals import (
    FRAME_SAMPLES,
    VAD_SR,
    ConfigError,
    LabelsError,
    Metrics,
    SegmentConfig,
    evaluate_session,
    n_frames,
    parse_config,
    parse_labels,
    postprocess,
    segment_raw,
)

FW_NAME = "fw_params_voxcut_postproc"
FW_NOTE = "параметры FW, постобработка VoxCut"
# D4: faster-whisper 1.2.1 VadOptions() run through the VoxCut pipeline on the same probs
FW_CONFIG = SegmentConfig(threshold=0.5, neg_threshold=0.35, min_speech_ms=0,
                          min_silence_ms=2000, pad_ms=400, merge_gap_ms=0)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def load_probs(probs_dir: Path) -> tuple[dict, np.ndarray]:
    """probs.json + probs.npy, checked against each other (D6)."""
    meta = load_json(probs_dir / "probs.json")
    probs = np.load(probs_dir / "probs.npy")
    if len(probs) != meta["n_frames"] or n_frames(meta["n16k"]) != meta["n_frames"]:
        raise RunError(f"{probs_dir}: в probs.npy {len(probs)} кадров, в probs.json "
                       f"n_frames={meta['n_frames']}, n16k={meta['n16k']}")
    return meta, probs


def _p_stats(probs: np.ndarray, s16: int, e16: int) -> tuple[float, float]:
    window = probs[s16 // FRAME_SAMPLES:-(-e16 // FRAME_SAMPLES)]
    return (float(window.max()), float(window.mean())) if len(window) else (0.0, 0.0)


def _mono(channel):
    return None if channel == "mono" else channel


def segment(probs_dir: str, cfg: SegmentConfig, meta: dict | None = None) -> Path:
    """seg-<cfg_sha8>/ with run.json (always), raw.jsonl, manifest.jsonl; reused if present."""
    probs_dir = Path(probs_dir)
    pj, probs = load_probs(probs_dir)
    # channel in the config only matters when set: it must name the channel the probs used
    if cfg.channel is not None and _mono(cfg.channel) != _mono(pj["channel"]):
        raise RunError(f"конфиг: channel={cfg.channel!r}, а probs посчитаны для "
                       f"channel={pj['channel']!r} ({probs_dir})")
    clean_orphans(probs_dir)
    probs_sha = hashlib.sha256((probs_dir / "probs.npy").read_bytes()).hexdigest()
    env = {k: pj[k] for k in ENV_KEYS}
    dst = probs_dir / f"seg-{cfg.sha256()[:8]}"
    if dst.exists():
        run = load_json(dst / "run.json")
        if run["probs_sha256"] != probs_sha:
            raise RunError(f"{dst}: probs.npy подменён после сегментации. "
                           f"Удалите каталог: rm -rf {dst}")
        check_env(run["env"], env, dst)
        return dst

    params = cfg.effective()
    sr, n_src = pj["source_sr"], pj["source_n_samples"]
    raw = segment_raw(probs.tolist(), params["threshold"], params["neg_threshold"],
                      params["min_speech_ms"], params["min_silence_ms"], pj["n16k"])
    final = postprocess(raw, params["merge_gap_ms"], params["pad_ms"], sr, n_src)
    source = {"source_path": pj["source_path"], "source_sha256": pj["source_sha256"],
              "ffmpeg_filter": pj["ffmpeg_filter"], "ffmpeg_version": pj["ffmpeg_version"],
              "source_sr": sr}
    raw_rows = []
    for s16, e16 in raw:
        p_max, p_mean = _p_stats(probs, s16, e16)
        raw_rows.append({"start_16k": s16, "end_16k": e16, "start_s": s16 / VAD_SR,
                         "end_s": e16 / VAD_SR, "p_max": p_max, "p_mean": p_mean})
    rows = []
    for s, e in final:
        p_max, p_mean = _p_stats(probs, s * VAD_SR // sr, -(-e * VAD_SR // sr))
        rows.append(source | {"start_sample": s, "end_sample": e, "start_s": s / sr,
                              "end_s": e / sr, "duration_s": (e - s) / sr, "branch": "raw",
                              "p_max": p_max, "p_mean": p_mean} | params
                    | {"model_version": pj["model_version"], "audio_path": None})
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.mkdir()
    write_jsonl(tmp / "raw.jsonl", raw_rows)
    write_jsonl(tmp / "manifest.jsonl", rows)
    run = {
        "status": "ok",
        "source": {"path": pj["source_path"], "sha256": pj["source_sha256"],
                   "n_samples": n_src, "sr": sr, "channels": pj["channels"],
                   "channel": pj["channel"], "codec": pj["codec_name"]},
        "config": params,
        "config_sha256": cfg.sha256(),
        "meta": meta or {},
        "counts": {"raw": len(raw), "final": len(final)},
        "probs_sha256": probs_sha,
        "env": env,
    }
    (tmp / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))  # last
    os.replace(tmp, dst)
    return dst


def evaluate(seg_dir: str, labels_path: str) -> tuple[Metrics, dict]:
    """Metrics of one session; the seg-dir is not modified (D19)."""
    seg_dir = Path(seg_dir)
    run = load_json(seg_dir / "run.json")
    pj = load_json(seg_dir.parent / "probs.json")
    rows = read_jsonl(seg_dir / "manifest.jsonl")
    sha = run["source"]["sha256"]
    if pj["source_sha256"] != sha or any(r["source_sha256"] != sha for r in rows):
        raise RunError(f"{seg_dir}: source_sha256 манифеста, run.json и probs.json не совпадают")
    sr = run["source"]["sr"]
    labels = parse_labels(Path(labels_path).read_text(),
                          duration_s=run["source"]["n_samples"] / sr)
    final = [(r["start_sample"] / sr, r["end_sample"] / sr) for r in rows]
    raw = [(r["start_s"], r["end_s"]) for r in read_jsonl(seg_dir / "raw.jsonl")]
    reference = {"me_events": len(labels["me"]),
                 "me_sec": sum(e - s for s, e in labels["me"]),
                 "scored_sec": sum(e - s for s, e in labels["scored"])}
    return evaluate_session(final, labels, raw_s=raw), reference


def fmt(x: float, digits: int = 3) -> str:
    return "n/a" if isinstance(x, float) and math.isnan(x) else f"{x:.{digits}f}"


COLUMNS = ("config", "session", "recall_sec", "recall_events", "fully_missed", "FA_asr_min_h",
           "FA_bound_min_h", "neutral_min_h", "clip_min_h", "clips_h", "coverage", "raw_recall")


def metrics_row(config: str, session: str, m: Metrics) -> list[str]:
    cov = fmt(m.coverage) + (" ≈весь файл" if m.whole_file else "")
    return [config, session, fmt(m.recall_sec), fmt(m.recall_events), str(len(m.fully_missed)),
            fmt(m.fa_asr_min_h, 2), fmt(m.fa_bound_min_h, 2), fmt(m.neutral_min_h, 2),
            fmt(m.clip_min_h, 2), fmt(m.clips_per_h, 1), cov, fmt(m.raw_recall_sec)]


def print_table(rows: list[list[str]]) -> None:
    print("\t".join(COLUMNS))
    for r in rows:
        print("\t".join(r))


def print_misses(m: Metrics) -> None:
    for s, e in m.fully_missed:
        print(f"пропущено целиком: {s:.2f}–{e:.2f} с")
    for s, e, cov in m.truncated:
        print(f"обрезано: {s:.2f}–{e:.2f} с, покрыто {cov:.0%}")


def cmd_probs(a) -> None:
    channel = int(a.channel) if a.channel and a.channel.isdigit() else a.channel
    print(compute_probs(a.source, a.out, channel))


def read_config(path: str) -> tuple[SegmentConfig, dict]:
    cfg, meta = parse_config(Path(path).read_text())
    return cfg, {"name": meta.get("name", Path(path).stem), "note": meta.get("note", "")}


def cmd_segment(a) -> None:
    print(segment(a.probs_dir, *read_config(a.config)))


def cmd_detect(a) -> None:
    cfg, meta = read_config(a.config)
    print(segment(compute_probs(a.source, a.out, cfg.channel), cfg, meta))


def cmd_evaluate(a) -> None:
    m, ref = evaluate(a.seg_dir, a.labels)
    print(f"эталон: событий me {ref['me_events']}, речь me {ref['me_sec']:.1f} с, "
          f"scored {ref['scored_sec']:.1f} с")
    run = load_json(Path(a.seg_dir) / "run.json")
    print_table([metrics_row(run["meta"].get("name", ""), Path(a.labels).name, m)])
    print_misses(m)


def cmd_sweep(a) -> None:
    configs = [read_config(p) for p in a.configs]
    configs.append((FW_CONFIG, {"name": FW_NAME, "note": FW_NOTE}))
    rows = []
    for cfg, meta in configs:
        total = Metrics()
        for probs_dir, labels in a.session:
            m, _ = evaluate(segment(probs_dir, cfg, meta), labels)
            total += m
            rows.append(metrics_row(meta["name"], Path(probs_dir).name, m))
        rows.append(metrics_row(meta["name"], "ALL", total))
    print_table(rows)
    print(f"{FW_NAME}: {FW_NOTE}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voxcut")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("probs", help="проверить источник и посчитать вероятности Silero")
    s.add_argument("source")
    s.add_argument("--out", required=True)
    s.add_argument("--channel", help='"mono", "mix" или индекс канала (D21)')
    s.set_defaults(func=cmd_probs)
    s = sub.add_parser("segment", help="интервалы из probs-каталога по конфигу")
    s.add_argument("probs_dir")
    s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_segment)
    s = sub.add_parser("detect", help="probs + segment")
    s.add_argument("source")
    s.add_argument("--config", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_detect)
    s = sub.add_parser("evaluate", help="метрики seg-каталога по разметке")
    s.add_argument("seg_dir")
    s.add_argument("labels")
    s.set_defaults(func=cmd_evaluate)
    s = sub.add_parser("sweep", help="конфиги × сессии → сводная таблица (D24)")
    s.add_argument("configs", nargs="+")
    s.add_argument("--session", nargs=2, action="append", required=True,
                   metavar=("PROBS_DIR", "LABELS"))
    s.set_defaults(func=cmd_sweep)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        a.func(a)
    except (SourceError, FFmpegError, RunError, ConfigError, LabelsError,
            FileNotFoundError) as e:
        print(f"voxcut: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
