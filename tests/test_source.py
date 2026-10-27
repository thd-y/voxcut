import struct

import numpy as np
import pytest
import soundfile as sf

from voxcut.audio import (
    FFmpegError,
    SourceError,
    check_source,
    check_wav_structure,
    convert_command,
    ffmpeg_stream,
    vad_filter,
)

SR = 8000


def write_wav(path, n=SR * 2, channels=1, fmt="WAV", subtype="PCM_16", data=None):
    if data is None:
        data = np.zeros((n, channels), dtype=np.float32)
    sf.write(path, data, SR, format=fmt, subtype=subtype)
    return path


def data_size_offset(raw: bytes) -> int:
    return raw.index(b"data") + 4


def lie_about_length(raw: bytes, new_size: int) -> bytes:
    off = data_size_offset(raw)
    return raw[:off] + struct.pack("<I", new_size) + raw[off + 4:]


def append_list_chunk(raw: bytes) -> bytes:
    body = b"INFOISFT\x05\x00\x00\x00test\x00\x00"
    return raw + b"LIST" + struct.pack("<I", len(body)) + body


# --- D15: RIFF walk (fast: soundfile only) ---

@pytest.mark.parametrize("mutate", [
    lambda raw: raw,
    append_list_chunk,  # LIST после data проходит
])
def test_wav_structure_ok(tmp_path, mutate):
    p = write_wav(tmp_path / "a.wav")
    p.write_bytes(mutate(p.read_bytes()))
    check_wav_structure(str(p))


def test_rf64_ok(tmp_path):
    check_wav_structure(str(write_wav(tmp_path / "a.wav", fmt="RF64")))


def test_rf64_short_ds64_refused(tmp_path):
    p = write_wav(tmp_path / "a.wav", fmt="RF64")
    raw = p.read_bytes()
    off = raw.index(b"ds64") + 4
    p.write_bytes(raw[:off] + struct.pack("<I", 8) + raw[off + 4:])
    with pytest.raises(SourceError, match="чанк b'ds64' короче 16 байт") as e:
        check_wav_structure(str(p))
    assert e.value.code == "source_wav_broken"


@pytest.mark.parametrize("mutate, needle", [
    (lambda raw: lie_about_length(raw, SR),
     "длина по заголовку 4000 сэмплов (0.5 с), по размеру файла 16000 сэмплов (2.0 с)"),
    (lambda raw: lie_about_length(raw, 0), "0x0"),
    (lambda raw: lie_about_length(raw, 0xFFFFFFFF), "0xffffffff"),
    (lambda raw: lie_about_length(raw, SR * 4 * 2), "выходит за конец файла"),
    (lambda raw: raw + b"\x01\x02\x03", "лишних байт"),
])
def test_broken_wav_refused_with_lengths_and_command(tmp_path, mutate, needle):
    p = write_wav(tmp_path / "a.wav")
    p.write_bytes(mutate(p.read_bytes()))
    with pytest.raises(SourceError) as e:
        check_wav_structure(str(p))
    assert e.value.code == "source_wav_broken"
    assert needle in str(e.value)
    assert "ffmpeg -ignore_length 1 -i" in str(e.value)


def short_fmt(raw: bytes) -> bytes:
    off = raw.index(b"fmt ") + 4
    return raw[:off] + struct.pack("<I", 2) + raw[off + 4:off + 6]


@pytest.mark.parametrize("mutate, needle, code", [
    (lambda raw: b"", "not a RIFF/RF64 WAVE", "source_not_riff"),  # диктофон создал файл и выключился
    (lambda raw: raw[:5], "not a RIFF/RF64 WAVE", "source_not_riff"),
    (lambda raw: raw[:30], "выходит за конец файла", "source_wav_broken"),  # обрыв внутри fmt
    (short_fmt, "чанк b'fmt ' короче 14 байт", "source_wav_broken"),
])
def test_truncated_wav_refused_not_crashed(tmp_path, mutate, needle, code):
    p = write_wav(tmp_path / "a.wav")
    p.write_bytes(mutate(p.read_bytes()))
    with pytest.raises(SourceError, match=needle) as e:
        check_wav_structure(str(p))
    assert e.value.code == code


# --- D21: channel ---

@pytest.mark.parametrize("channel, channels, expected", [
    (None, 1, "aresample=16000:resampler=swr"),
    ("mono", 1, "aresample=16000:resampler=swr"),
    (0, 2, "pan=mono|c0=c0,aresample=16000:resampler=swr"),
    (1, 2, "pan=mono|c0=c1,aresample=16000:resampler=swr"),
    ("mix", 2, "pan=mono|c0=0.5*c0+0.5*c1,aresample=16000:resampler=swr"),
])
def test_vad_filter(channel, channels, expected):
    assert vad_filter(channel, channels) == expected


@pytest.mark.parametrize("channel, channels", [
    (None, 2), ("mono", 2), (2, 2), (-1, 2), (True, 2), ("left", 2),
])
def test_vad_filter_refuses(channel, channels):
    with pytest.raises(SourceError):
        vad_filter(channel, channels)


# --- D18 + U6: refusal command ---

@pytest.mark.parametrize("codec, expected", [
    ("pcm_s16le", "ffmpeg -i x.wav -c:a flac x.wav.flac"),
    ("pcm_s24le", "ffmpeg -i x.wav -c:a flac x.wav.flac"),
    ("pcm_f32le", "ffmpeg -i x.wav -c:a pcm_f32le -rf64 auto x.wav.fixed.wav"),
    ("pcm_f64le", "ffmpeg -i x.wav -c:a pcm_f64le -rf64 auto x.wav.fixed.wav"),
])
def test_convert_command(codec, expected):
    assert convert_command("x.wav", codec).startswith(expected)


# --- full check against real ffmpeg ---

@pytest.mark.slow
@pytest.mark.parametrize("fmt, subtype, codec", [
    ("WAV", "PCM_16", "pcm_s16le"),
    ("WAV", "PCM_24", "pcm_s24le"),
    ("WAV", "FLOAT", "pcm_f32le"),
    ("RF64", "PCM_16", "pcm_s16le"),
    ("FLAC", "PCM_24", "flac"),
])
def test_check_source_ok(tmp_path, fmt, subtype, codec):
    p = write_wav(tmp_path / f"a.{fmt.lower()}", n=SR * 3 + 7, channels=2, fmt=fmt,
                  subtype=subtype)
    info = check_source(str(p))
    assert (info.n_samples, info.channels, info.sr, info.codec_name) == (SR * 3 + 7, 2, SR, codec)


@pytest.mark.slow
def test_check_source_refuses_broken_wav(tmp_path):
    p = write_wav(tmp_path / "a.wav")
    p.write_bytes(lie_about_length(p.read_bytes(), SR * 2))
    with pytest.raises(SourceError, match="по размеру файла") as e:
        check_source(str(p))
    assert e.value.code == "source_wav_broken"


@pytest.mark.slow
def test_check_source_refuses_other_container(tmp_path):
    p = write_wav(tmp_path / "a.ogg", fmt="OGG", subtype="VORBIS")
    with pytest.raises(SourceError, match="ffmpeg -i .* -c:a flac") as e:
        check_source(str(p))
    assert e.value.code == "source_format"


@pytest.mark.slow
def test_check_source_refuses_lost_flac_frames(tmp_path):
    # Порченые заголовки кадров: ffmpeg молча теряет сэмплы, ловит frames == n_ffmpeg.
    rng = np.random.default_rng(0)
    p = write_wav(tmp_path / "a.flac", fmt="FLAC", data=rng.uniform(-0.5, 0.5, (SR * 3, 1)))
    raw = bytearray(p.read_bytes())
    for i in range(len(raw) // 3, 2 * len(raw) // 3, 7):
        raw[i] ^= 0xFF
    p.write_bytes(bytes(raw))
    with pytest.raises((SourceError, FFmpegError)):
        check_source(str(p))


@pytest.mark.slow
def test_antiphase_stereo_mix_cancels_channel_sees(tmp_path):
    t = np.arange(SR * 2) / SR
    s = 0.5 * np.sin(2 * np.pi * 300 * t)
    p = write_wav(tmp_path / "a.wav", data=np.stack([s, -s], axis=1))
    info = check_source(str(p))
    with pytest.raises(SourceError):
        vad_filter(None, info.channels)

    def rms(channel):
        args = ["-i", str(p), "-af", vad_filter(channel, info.channels), "-f", "f32le", "-"]
        with ffmpeg_stream(args) as out:
            x = np.frombuffer(out.read(), dtype=np.float32)
        return float(np.sqrt(np.mean(x ** 2)))

    assert rms("mix") < 1e-3
    assert rms(0) == pytest.approx(0.5 / np.sqrt(2), rel=0.02)


@pytest.mark.slow
def test_check_source_refuses_header_shorter_than_decode(tmp_path):
    # STREAMINFO занижает total_samples: soundfile верит заголовку, ffmpeg декодирует всё.
    p = write_wav(tmp_path / "a.flac", n=SR * 3, fmt="FLAC")
    raw = bytearray(p.read_bytes())
    total = SR * 3 - 1000
    raw[8 + 13] = (raw[8 + 13] & 0xF0) | (total >> 32)
    raw[8 + 14:8 + 18] = struct.pack(">I", total & 0xFFFFFFFF)
    p.write_bytes(bytes(raw))
    with pytest.raises(SourceError, match=f"soundfile видит {total} сэмплов, ffmpeg декодирует") as e:
        check_source(str(p))
    assert e.value.code == "source_frames_mismatch"


@pytest.mark.slow
def test_check_source_refuses_non_pcm_codec(tmp_path):
    p = write_wav(tmp_path / "a.wav", subtype="IMA_ADPCM")
    with pytest.raises(SourceError, match="кодек adpcm_ima_wav") as e:
        check_source(str(p))
    assert e.value.code == "source_codec"
