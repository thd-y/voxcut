"""Everything that touches ffmpeg, soundfile or the model (D1)."""

import hashlib
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import numpy as np
import soundfile as sf

from voxcut.intervals import (
    FRAME_SAMPLES,
    GAP_MAX_S,
    GAP_MIN_S,
    PAUSE_MAX_S,
    RESET_PERIOD_FRAMES,
    VAD_SR,
    SessionError,
    data_bytes_ok,
    file_start,
    gap_s,
    meta_key,
    mtime_warnings,
    n_frames,
    parse_start,
    pick_chain,
    seam_frames,
    session_name,
    split_chains,
    start_local,
)


class FFmpegError(RuntimeError):
    pass


def find_ffmpeg() -> tuple[str, str]:
    """The one ffmpeg check (D2): returns (path, first line of `ffmpeg -version`)."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise FFmpegError("ffmpeg не найден, установите: sudo apt install ffmpeg")
    r = subprocess.run(
        [path, "-hide_banner", "-version"], check=False, capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout:
        raise FFmpegError(f"{path} -version завершился с кодом {r.returncode}: {r.stderr.strip()}")
    return path, r.stdout.splitlines()[0]


@contextmanager
def ffmpeg_stream(args: list[str]):
    """Run ffmpeg with `args` and yield its stdout as a binary stream.

    A nonzero exit or any error-level message is a refusal (C2), raised when the block ends,
    so the caller never reaches publication. ffmpeg 6.1 exits 0 on decode errors, hence stderr.
    """
    path, _ = find_ffmpeg()
    cmd = [path, "-nostdin", "-hide_banner", "-v", "error", *args]
    # stderr goes to a file, not a pipe: a full stderr pipe would stall the stdout stream
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            yield proc.stdout
        except BaseException:
            proc.kill()
            raise
        finally:
            proc.stdout.close()
            proc.wait()
        err.seek(0)
        msg = err.read().decode(errors="replace").strip()
        if proc.returncode != 0 or msg:
            raise FFmpegError(
                f"ffmpeg завершился с кодом {proc.returncode}: {msg}\nкоманда: {shlex.join(cmd)}"
            )


def probe_codec(src: str) -> str:
    """codec_name of the first audio stream, same refusal rules as above."""
    find_ffmpeg()
    path = shutil.which("ffprobe")
    if path is None:
        raise FFmpegError("ffprobe не найден, установите: sudo apt install ffmpeg")
    cmd = [path, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name", "-of", "json", src]
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if r.returncode != 0 or r.stderr.strip():
        raise FFmpegError(f"ffprobe завершился с кодом {r.returncode}: {r.stderr.strip()}"
                          f"\nкоманда: {shlex.join(cmd)}")
    streams = json.loads(r.stdout)["streams"]
    if not streams:
        raise SourceError(f"{src}: нет аудиодорожки")
    return streams[0]["codec_name"]


class SourceError(RuntimeError):
    """The source is unfit for sample-exact work; the message carries the fix command."""


@dataclass(frozen=True)
class SourceInfo:
    sr: int
    channels: int
    n_samples: int  # counted by ffmpeg decode (D5), equals sf.info frames (D18)
    codec_name: str
    format: str
    subtype: str


def convert_command(src: str, codec_name: str) -> str:
    """Refusal command (D18, U6): float stays float, everything else goes to FLAC."""
    if codec_name.startswith(("pcm_f32", "pcm_f64")):
        codec = "pcm_f64le" if codec_name.startswith("pcm_f64") else "pcm_f32le"
        cmd = ["ffmpeg", "-i", src, "-c:a", codec, "-rf64", "auto", f"{src}.fixed.wav"]
    else:
        cmd = ["ffmpeg", "-i", src, "-c:a", "flac", f"{src}.flac"]
    return f"{shlex.join(cmd)}\nдальше источник — этот файл"


def _printable(cid: bytes) -> bool:
    return all(0x20 <= b <= 0x7E for b in cid)


def check_wav_structure(src: str) -> None:
    """D15: both ffmpeg and libsndfile trust the WAV header, so walk the RIFF chunks ourselves.

    Every byte after `data` must parse into valid chunks up to EOF; data size 0 or 0xFFFFFFFF
    (outside RF64) means a header the recorder never finished.
    """
    size = os.path.getsize(src)
    fix = shlex.join(["ffmpeg", "-ignore_length", "1", "-i", src, "-c:a", "copy",
                      "-rf64", "auto", f"{src}.fixed.wav"])
    with open(src, "rb") as f:
        magic, _, wave = struct.unpack("<4sI4s", f.read(12))
        if magic not in (b"RIFF", b"RF64") or wave != b"WAVE":
            raise SourceError(f"{src}: не RIFF/RF64 WAVE")
        sr = block_align = data_start = data_size = ds64_data = None
        bad = None
        pos = 12
        while pos < size:
            if size - pos < 8:
                bad = f"{size - pos} лишних байт в конце файла"
                break
            f.seek(pos)
            cid, csize = struct.unpack("<4sI", f.read(8))
            if not _printable(cid):
                bad = f"мусор вместо чанка на смещении {pos}"
                break
            if cid == b"ds64":
                ds64_data = struct.unpack("<QQ", f.read(16))[1]
            elif cid == b"fmt ":
                _, _, sr, _, block_align = struct.unpack("<HHIIH", f.read(14))
            elif cid == b"data" and data_start is None:
                if magic == b"RF64" and csize == 0xFFFFFFFF and ds64_data is not None:
                    csize = ds64_data
                data_start, data_size = pos + 8, csize
                if csize in (0, 0xFFFFFFFF):
                    bad = f"размер data в заголовке = {csize:#x}"
                    break
            end = pos + 8 + csize
            if end > size:
                bad = f"чанк {cid!r} на смещении {pos} выходит за конец файла"
                break
            pos = end + (csize & 1)
        if bad is None and (data_start is None or not block_align):
            bad = "нет чанка fmt или data"
    if bad is None:
        return
    lengths = ""
    if data_start is not None and block_align and sr:
        by_header = data_size // block_align
        by_file = (size - data_start) // block_align
        lengths = (f"; длина по заголовку {by_header} сэмплов ({by_header / sr:.1f} с), "
                   f"по размеру файла {by_file} сэмплов ({by_file / sr:.1f} с)")
    raise SourceError(f"{src}: битая структура WAV: {bad}{lengths}.\nПересохраните: {fix}")


def count_samples(src: str, channels: int) -> int:
    """n_ffmpeg (D5): samples per channel as ffmpeg decodes them at the source rate."""
    n = 0
    with ffmpeg_stream(["-i", src, "-map", "0:a:0", "-f", "s16le", "-"]) as out:
        while chunk := out.read(1 << 20):
            n += len(chunk)
    return n // (2 * channels)


def check_source(src: str) -> SourceInfo:
    """Source suitability before the model runs (D15, D18): refusal carries a fix command."""
    with open(src, "rb") as f:
        if f.read(4) in (b"RIFF", b"RF64"):
            check_wav_structure(src)
    codec = probe_codec(src)
    fix = convert_command(src, codec)
    try:
        info = sf.info(src)
    except Exception as e:
        raise SourceError(f"{src}: soundfile не читает файл ({e}).\nКонвертируйте: {fix}") from e
    if info.format not in ("WAV", "WAVEX", "RF64", "FLAC"):
        raise SourceError(f"{src}: формат {info.format}, в v1 только WAV или FLAC."
                          f"\nКонвертируйте: {fix}")
    if not (codec.startswith("pcm_") or codec == "flac"):
        raise SourceError(f"{src}: кодек {codec}, сэмпл-точность не гарантирована."
                          f"\nКонвертируйте: {fix}")
    n = count_samples(src, info.channels)
    if n != info.frames:
        raise SourceError(f"{src}: soundfile видит {info.frames} сэмплов, ffmpeg декодирует {n}."
                          f"\nКонвертируйте: {fix}")
    return SourceInfo(info.samplerate, info.channels, n, codec, info.format, info.subtype)


def vad_filter(channel: str | int | None, channels: int) -> str:
    """-af string for the 16 kHz mono VAD copy (D2, D21). Stereo needs an explicit choice."""
    resample = "aresample=16000:resampler=swr"
    if channel is None or channel == "mono":
        if channels > 1:
            raise SourceError(
                f"в источнике {channels} канала(ов): задайте channel = индекс 0..{channels - 1} "
                f'или "mix"')
        return resample
    if channel == "mix":
        weights = "+".join(f"{1 / channels:.6g}*c{i}" for i in range(channels))
        return f"pan=mono|c0={weights},{resample}"
    if isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel < channels:
        return f"pan=mono|c0=c{channel},{resample}"
    raise SourceError(f'channel = {channel!r}: ожидается "mono", "mix" или индекс 0..{channels - 1}')


def vad_probs(src: str, af: str, model=None, reset_at=(), reset_period=0) -> tuple[np.ndarray, int]:
    """Stream the 16 kHz copy through Silero frame by frame: (probs float32, n16k).

    inference_mode is mandatory: without it autograd keeps ~21 KB per frame (C1).
    The model state is reset before each frame in reset_at, i.e. at session seams (D29).
    With reset_period > 0 a second model on the same frames also resets every reset_period
    frames, and the elementwise max of both passes is returned (D31): on long music the
    state goes deaf to whisper, while a fresh state sometimes misses what context caught.
    Pass B always uses the default JIT model, even when `model` is passed.
    """
    import torch

    torch.set_num_threads(TORCH_THREADS)  # C3: reproducible probs
    from silero_vad import load_silero_vad

    if model is None:
        model = load_silero_vad()
    model.reset_states()
    second = load_silero_vad() if reset_period else None
    if second is not None:
        second.reset_states()
    resets = set(reset_at)
    probs, probs_b, n16k = [], [], 0
    args = ["-i", src, "-map", "0:a:0", "-af", af, "-f", "f32le", "-"]
    with torch.inference_mode(), ffmpeg_stream(args) as out:
        while buf := out.read(FRAME_SAMPLES * 4):
            x = np.frombuffer(buf, dtype=np.float32)
            n16k += len(x)
            if len(x) < FRAME_SAMPLES:
                x = np.pad(x, (0, FRAME_SAMPLES - len(x)))
            n, frame = len(probs), torch.from_numpy(x.copy())
            if n in resets:
                model.reset_states()
            probs.append(model(frame, VAD_SR).item())
            if second is not None:
                if n in resets or (n and n % reset_period == 0):
                    second.reset_states()
                probs_b.append(second(frame, VAD_SR).item())
    probs = np.asarray(probs, dtype=np.float32)
    return (np.maximum(probs, np.asarray(probs_b, dtype=np.float32)) if second is not None
            else probs), n16k


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def probs_dir_name(src: str, sha: str, channel: str | int | None) -> str:
    """<stem>.<sha8>[.c<i>|.mix].raw: channel changes the probs, so it is part of the key (D21)."""
    tag = "" if channel in (None, "mono") else (".mix" if channel == "mix" else f".c{channel}")
    return f"{Path(src).stem}.{sha[:8]}{tag}.raw"


TORCH_THREADS = 1
ENV_KEYS = ("silero_vad_version", "model_version", "ffmpeg_version", "ffmpeg_filter",
            "torch_threads")


class RunError(RuntimeError):
    """A published run dir does not match what this run would produce (D19)."""


def fresh_tmp(dst: Path) -> Path:
    """Empty <dst>.tmp to publish from; an orphan of an interrupted run is removed (D19).

    Only our own name: other *.tmp dirs may be the owner's or a parallel run's.
    """
    tmp = dst.with_name(dst.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    return tmp


def current_env(af: str) -> dict:
    return {"silero_vad_version": version("silero-vad"), "model_version": version("silero-vad"),
            "ffmpeg_version": find_ffmpeg()[1], "ffmpeg_filter": af,
            "torch_threads": TORCH_THREADS}


def check_env(stored: dict, current: dict, run_dir: Path) -> None:
    diff = [f"{k}: было {stored.get(k)!r}, сейчас {v!r}" for k, v in current.items()
            if stored.get(k) != v]
    if diff:
        raise RunError(f"{run_dir}: окружение изменилось ({'; '.join(diff)}).\n"
                       f"Результат не переиспользуется. Удалите каталог и запустите заново: "
                       f"rm -rf {shlex.quote(str(run_dir))}")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise RunError(f"нет {path}: каталог неполный или не тот") from None


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def check_reset_period(meta: dict, probs_dir: Path) -> None:
    """probs from before the second Silero pass (D31) are never reused, by probs or segment."""
    if meta.get("reset_period_frames") != RESET_PERIOD_FRAMES:
        raise RunError(f"{probs_dir}: probs посчитаны без второго прохода Silero (D31).\n"
                       f"Удалите каталог и запустите заново: rm -rf {shlex.quote(str(probs_dir))}")


def read_session(src: str, sha: str) -> dict | None:
    path = Path(src).parent / "session.json"
    if not path.exists():
        return None
    session = load_json(path)
    if session["session_sha256"] != sha:
        raise RunError(f"{path}: session_sha256 не совпадает с sha256 {src}; "
                       "сессия не от этого файла")
    return session


def compute_probs(src: str, out_dir: str, channel: str | int | None = None) -> Path:
    """`probs`: check the source, run Silero, publish <dir>.tmp -> os.replace (D6, D19).

    An existing dir for the same source and channel is reused after the env check.
    A session.json next to the source (D25) is checked by sha and copied into probs.json, so
    segment never needs the session dir; with identical channels it picks channel 0 (D27).
    """
    sha = sha256_file(src)
    session = read_session(src, sha)
    channel_source = None
    if channel is None and session and session["channels_identical"]:
        channel, channel_source = 0, "session.channels_identical"
    dst = Path(out_dir) / probs_dir_name(src, sha, channel)
    resets = seam_frames([x["offset_samples"] for x in session["seams"]],
                         session["sr"]) if session else None
    if dst.exists():
        meta = load_json(dst / "probs.json")
        check_env(meta, current_env(vad_filter(channel, meta["channels"])), dst)
        if meta.get("session") != session:
            raise RunError(f"{dst}: блок session в probs.json не совпадает с session.json "
                           f"рядом с источником.\nУдалите каталог и запустите заново: "
                           f"rm -rf {shlex.quote(str(dst))}")
        check_reset_period(meta, dst)
        if meta.get("seam_reset_frames") != resets:
            raise RunError(f"{dst}: probs посчитаны без сброса Silero на стыках (D29).\n"
                           f"Удалите каталог и запустите заново: rm -rf {shlex.quote(str(dst))}")
        return dst
    info = check_source(src)
    if session and session["n_samples"] != info.n_samples:
        raise RunError(f"{src}: в session.json {session['n_samples']} сэмплов, "
                       f"в источнике {info.n_samples}")
    af = vad_filter(channel, info.channels)
    tmp = fresh_tmp(dst)
    probs, n16k = vad_probs(src, af, reset_at=resets or (), reset_period=RESET_PERIOD_FRAMES)
    if len(probs) != n_frames(n16k):
        raise RuntimeError(f"кадров {len(probs)}, ожидалось n_frames({n16k}) = {n_frames(n16k)}")
    np.save(tmp / "probs.npy", probs)
    import torch

    meta = current_env(af) | {
        "source_path": str(Path(src).resolve()),
        "source_sha256": sha,
        "source_sr": info.sr,
        "source_n_samples": info.n_samples,
        "channels": info.channels,
        "channel": channel,
        "codec_name": info.codec_name,
        "format": info.format,
        "subtype": info.subtype,
        "vad_sr": VAD_SR,
        "frame_samples": FRAME_SAMPLES,
        "n16k": n16k,
        "n_frames": len(probs),
        "model_name": "silero_vad.jit",
        "torch_version": torch.__version__,
        "reset_period_frames": RESET_PERIOD_FRAMES,
    }
    if channel_source:
        meta["channel_source"] = channel_source
    if session:
        meta["seam_reset_frames"] = resets
        meta["session"] = session
    (tmp / "probs.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))  # last
    os.replace(tmp, dst)
    return dst


EXPORT_BLOCK = 1 << 20  # frames per block (C7)
_DTYPES = {"PCM_16": "int16", "PCM_24": "int32", "PCM_32": "int32", "FLOAT": "float32",
           "DOUBLE": "float64"}


def export_clips(seg_dir: str) -> Path:
    """`export`: cut final intervals from the source into seg-dir/clips/ (D18, C7, D19).

    Blocks in the source's native dtype, so RSS does not grow with the clip length; clips keep
    the container, subtype and all channels of the source.
    """
    seg_dir = Path(seg_dir)
    run = load_json(seg_dir / "run.json")
    rows = read_jsonl(seg_dir / "manifest.jsonl")
    src = run["source"]["path"]
    if sha256_file(src) != run["source"]["sha256"]:
        raise RunError(f"{src}: sha256 не совпадает с манифестом {seg_dir}, источник изменился")
    dst = seg_dir / "clips"
    if dst.exists():
        raise RunError(f"{dst} уже существует: клипы не перезаписываются (D19)")
    info = sf.info(src)
    dtype = _DTYPES.get(info.subtype, "int16")  # 8-bit PCM is exact through int16
    ext = "flac" if info.format == "FLAC" else "wav"
    seams_path = seg_dir / "seams.jsonl"  # D25: windows around seams, only for sessions
    jobs = [("", rows, "manifest.jsonl")]
    if seams_path.exists():
        jobs.insert(0, ("seams/", read_jsonl(seams_path), "seams.jsonl"))
    tmp = fresh_tmp(dst)
    with sf.SoundFile(src) as f:
        for sub, job_rows, manifest in jobs:
            (tmp / sub).mkdir(exist_ok=True)
            out_rows = []
            for i, row in enumerate(job_rows):
                start, end = row["start_sample"], min(row["end_sample"], info.frames)  # D5
                name = f"{sub}{i:04d}_{start}_{end}.{ext}"
                f.seek(start)
                with sf.SoundFile(tmp / name, "w", info.samplerate, info.channels,
                                  subtype=info.subtype, format=info.format) as out:
                    left = end - start
                    while left > 0:
                        block = f.read(min(EXPORT_BLOCK, left), dtype=dtype, always_2d=True)
                        if not len(block):
                            break
                        out.write(block)
                        left -= len(block)
                out_rows.append(row | {"end_sample": end, "audio_path": f"clips/{name}"})
            write_jsonl(tmp / manifest, out_rows)  # manifest.jsonl is last
    os.replace(tmp, dst)
    return dst


# --- session (D25–D27) ---

_SUBTYPE_BYTES = {"PCM_16": 2, "PCM_24": 3, "PCM_32": 4, "FLOAT": 4, "DOUBLE": 8}
SESSION_ENV_KEYS = ("voxcut_version", "soundfile_version", "libsndfile_version")  # D25: no ffmpeg


def session_env() -> dict:
    return {"voxcut_version": version("voxcut"), "soundfile_version": sf.__version__,
            "libsndfile_version": sf.__libsndfile_version__}


def channel_advice(identical: bool | None) -> list[str]:
    """D25, D27: no automatic mix advice, mix cancels speech in antiphase."""
    if identical is None:
        return []
    if identical:
        return ["каналы совпадают бит в бит: probs возьмёт канал 0 сам (или --channel 0)"]
    return [("ВНИМАНИЕ: каналы различаются. Выберите --channel 0 | 1 | mix; mix может гасить "
             "речь при противофазе, проверьте каналы на слух")]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def assemble_session(src_dir: str, out_dir: str, chain: int | None = None,
                     start: str | None = None, log=_log) -> Path:
    """`session`: hourly recorder files -> <out>/<name>.<sha8>.session/ (D25).

    <name>.wav is a bit-exact block copy; the originals are only read. Refusals come before
    any decode: names, RIFF structure, sf.info, chains, the 4 GiB RIFF limit.
    """
    real_start = parse_start(start) if start is not None else None
    wavs = sorted(p for p in Path(src_dir).iterdir() if p.is_file() and p.suffix.lower() == ".wav")
    if not wavs:
        raise SessionError(f"{src_dir}: нет WAV-файлов")
    foreign = [p.name for p in wavs if file_start(p.name) is None]
    if foreign:
        raise SessionError(f"{src_dir}: имена не по правилу диктофона YYYYMMDDHHMMSS.wav: "
                           f"{', '.join(foreign)}")
    for p in wavs:
        check_wav_structure(str(p))
    infos = [sf.info(str(p)) for p in wavs]
    parts = [(p.name, file_start(p.name), i.frames) for p, i in zip(wavs, infos, strict=True)]
    names = [p.name for p in wavs]
    idx = pick_chain(split_chains(parts, infos[0].samplerate), chain, names)
    files = [wavs[i] for i in idx]
    infos = [infos[i] for i in idx]
    parts = [parts[i] for i in idx]
    first = infos[0]
    for p, i in zip(files, infos, strict=True):
        if (i.samplerate, i.channels, i.subtype) != (first.samplerate, first.channels,
                                                       first.subtype):
            raise SessionError(f"{p}: {i.samplerate} Гц, {i.channels} кан., {i.subtype}; "
                               f"первый файл цепочки: {first.samplerate} Гц, {first.channels} "
                               f"кан., {first.subtype}")
    sr, channels, subtype = first.samplerate, first.channels, first.subtype
    if subtype not in _SUBTYPE_BYTES:
        raise SessionError(f"{files[0]}: subtype {subtype}, для сессии нужен PCM или FLOAT")
    mtime_ends = [datetime.fromtimestamp(p.stat().st_mtime) for p in files]
    offsets = [0]
    for _, _, n in parts:
        offsets.append(offsets[-1] + n)
    n_total = offsets.pop()
    rec_start = parts[0][1]
    real_start = real_start or rec_start
    clock_offset_s = (real_start - rec_start).total_seconds()
    seams = []
    for k in range(1, len(parts)):
        prev = parts[k - 1]
        gap = gap_s(prev, parts[k], sr)
        seams.append({"offset_samples": offsets[k], "kind": "split", "after": prev[0],
                      "before": parts[k][0], "gap_est_s": round(gap, 3)})
    log(f"инвентарь {src_dir}:")
    for (name, t, n), end, off in zip(parts, mtime_ends, offsets, strict=True):
        log(f"  {name}: старт по имени {t:%H:%M:%S}, по mtime − длительность "
            f"{end - timedelta(seconds=n / sr):%H:%M:%S}, {n / sr:.1f} с, смещение {off}")
    for s in seams:
        log(f"  стык {s['after']} → {s['before']}: разрыв по оценке {s['gap_est_s']:+.2f} с")
    log(f"  итого {len(parts)} файлов, {n_total / sr:.1f} с")
    for w in mtime_warnings(parts, mtime_ends, sr):
        log(f"ВНИМАНИЕ: {w}")
    if start is None:
        log("время сессии по часам диктофона; если часы были сбиты, повторите с --start")
    name = session_name(real_start, n_total, sr)
    key = meta_key([(p.name, p.stat().st_size, n) for p, (_, _, n) in zip(files, parts,
                                                                          strict=True)],
                   real_start.isoformat() if start is not None else None)
    dst = Path(out_dir) / f"{name}.{key}.session"
    wav_name = f"{name}.wav"
    if dst.exists():
        run = load_json(dst / "run.json")
        check_env(run["env"], session_env(), dst)
        meta = load_json(dst / "session.json")
        rm = f"rm -rf {shlex.quote(str(dst))}"
        if not (dst / wav_name).exists() or sf.info(str(dst / wav_name)).frames != n_total:
            raise RunError(f"{dst}: {wav_name} нет или его длина не {n_total} сэмплов.\n"
                           f"Удалите каталог и запустите заново: {rm}")
        for line in channel_advice(meta["channels_identical"]):
            log(line)
        return dst
    if not data_bytes_ok(n_total, channels, _SUBTYPE_BYTES[subtype]):
        raise SessionError(f"сессия {n_total / sr / 3600:.2f} ч не помещается в WAV "
                           f"(предел RIFF 4 ГиБ); разбейте папку на части")
    for p in files:
        check_source(str(p))  # D18: frames == n_ffmpeg for every file of the chain
    tmp = fresh_tmp(dst)
    dtype = _DTYPES[subtype]
    identical = True if channels > 1 else None
    file_rows = []
    with sf.SoundFile(tmp / wav_name, "w", sr, channels, subtype=subtype, format="WAV") as out:
        for p, (fname, t, n), end, off in zip(files, parts, mtime_ends, offsets, strict=True):
            sha = sha256_file(str(p))  # the copy below reads the same bytes from page cache
            with sf.SoundFile(str(p)) as f:
                while len(block := f.read(EXPORT_BLOCK, dtype=dtype, always_2d=True)):
                    if identical and not (block == block[:, :1]).all():
                        identical = False
                    out.write(block)
            file_rows.append({"name": fname, "path": str(p.resolve()), "sha256": sha,
                              "size_bytes": p.stat().st_size, "n_samples": n,
                              "offset_samples": off, "recorder_time": t.isoformat(),
                              "start_local": start_local(t, 0, sr, clock_offset_s),
                              "mtime_end": end.isoformat(timespec="seconds")})
    info = check_source(str(tmp / wav_name))
    if info.n_samples != n_total:
        raise SourceError(f"{tmp / wav_name}: {info.n_samples} сэмплов, ожидалось {n_total}")
    session = {
        "session_id": f"{name}.{key}",
        "name": name,
        "wav": wav_name,
        "session_sha256": sha256_file(str(tmp / wav_name)),
        "n_samples": n_total,
        "sr": sr,
        "channels": channels,
        "subtype": subtype,
        "channels_identical": identical,
        "files": file_rows,
        "seams": seams,
        "gap_min_s": GAP_MIN_S,
        "gap_max_s": GAP_MAX_S,
        "pause_max_s": PAUSE_MAX_S,
        "wallclock_source": "filename+start" if start is not None else "filename",
        "wallclock_precision_s": 1,
        "clock_offset_s": clock_offset_s,
    }
    (tmp / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=1))
    run = {"env": session_env(), "ffmpeg_version": find_ffmpeg()[1],  # for reference only
           "args": {"dir": str(Path(src_dir).resolve()), "chain": chain, "start": start}}
    (tmp / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))  # last
    os.replace(tmp, dst)
    for line in channel_advice(identical):
        log(line)
    return dst
