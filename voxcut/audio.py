"""Everything that touches ffmpeg, soundfile or the model (D1)."""

import json
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

import soundfile as sf


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


def ffprobe_audio(src: str) -> dict:
    """codec_name, sample_rate, channels of the first audio stream, same refusal rules as above."""
    find_ffmpeg()
    path = shutil.which("ffprobe")
    if path is None:
        raise FFmpegError("ffprobe не найден, установите: sudo apt install ffmpeg")
    cmd = [path, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", src]
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if r.returncode != 0 or r.stderr.strip():
        raise FFmpegError(f"ffprobe завершился с кодом {r.returncode}: {r.stderr.strip()}"
                          f"\nкоманда: {shlex.join(cmd)}")
    streams = json.loads(r.stdout)["streams"]
    if not streams:
        raise SourceError(f"{src}: нет аудиодорожки")
    st = streams[0]
    return {"codec_name": st["codec_name"], "sample_rate": int(st["sample_rate"]),
            "channels": int(st["channels"])}


class SourceError(RuntimeError):
    """The source is unfit for sample-exact work; the message carries the fix command."""


@dataclass(frozen=True)
class SourceInfo:
    path: str
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
    probe = ffprobe_audio(src)
    fix = convert_command(src, probe["codec_name"])
    try:
        info = sf.info(src)
    except Exception as e:
        raise SourceError(f"{src}: soundfile не читает файл ({e}).\nКонвертируйте: {fix}") from e
    if info.format not in ("WAV", "WAVEX", "RF64", "FLAC"):
        raise SourceError(f"{src}: формат {info.format}, в v1 только WAV или FLAC."
                          f"\nКонвертируйте: {fix}")
    codec = probe["codec_name"]
    if not (codec.startswith("pcm_") or codec == "flac"):
        raise SourceError(f"{src}: кодек {codec}, сэмпл-точность не гарантирована."
                          f"\nКонвертируйте: {fix}")
    n = count_samples(src, info.channels)
    if n != info.frames:
        raise SourceError(f"{src}: soundfile видит {info.frames} сэмплов, ffmpeg декодирует {n}."
                          f"\nКонвертируйте: {fix}")
    return SourceInfo(src, info.samplerate, info.channels, n, codec, info.format, info.subtype)


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
