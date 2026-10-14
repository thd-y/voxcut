import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from voxcut.audio import (
    FFmpegError,
    compute_probs,
    ffmpeg_stream,
    probs_dir_name,
    sha256_file,
    vad_filter,
    vad_probs,
)
from voxcut.intervals import n_frames

SR = 16000


def noise_wav(path, seconds, sr=SR, channels=1, seed=0):
    rng = np.random.default_rng(seed)
    sf.write(path, rng.uniform(-0.3, 0.3, (int(seconds * sr), channels)), sr, subtype="PCM_16")
    return str(path)


@pytest.mark.parametrize("n16k, expected", [(0, 0), (1, 1), (512, 1), (513, 2), (1024, 2)])
def test_n_frames(n16k, expected):
    assert n_frames(n16k) == expected


@pytest.mark.parametrize("channel, tag", [(None, ""), ("mono", ""), (0, ".c0"), ("mix", ".mix")])
def test_probs_dir_name_keys_channel(channel, tag):
    assert probs_dir_name("/x/rec 1.wav", "abcdef0123", channel) == f"rec 1.abcdef01{tag}.raw"


@pytest.fixture(scope="module")
def model():
    from silero_vad import load_silero_vad

    return load_silero_vad()


@pytest.mark.slow
def test_matches_silero_loop_bitwise(tmp_path, model):
    """D22b: our streaming loop == the loop inside silero get_speech_timestamps."""
    import torch
    from silero_vad import get_speech_timestamps

    src = noise_wav(tmp_path / "a.wav", 7.3, sr=44100)  # 7.3 с: последний кадр неполный
    af = vad_filter(None, 1)
    ours, n16k = vad_probs(src, af, model)
    assert np.array_equal(vad_probs(src, af, model)[0], ours)  # reset_states между файлами

    with ffmpeg_stream(["-i", src, "-af", af, "-f", "f32le", "-"]) as out:
        audio = torch.from_numpy(np.frombuffer(out.read(), dtype=np.float32).copy())

    class Recorder:
        def __init__(self):
            self.probs = []

        def reset_states(self):
            model.reset_states()

        def __call__(self, x, sr):
            p = model(x, sr)
            self.probs.append(p.item())
            return p

    rec = Recorder()
    with torch.inference_mode():
        get_speech_timestamps(audio, rec, sampling_rate=SR)
    assert n16k == len(audio) and len(ours) == n_frames(n16k)
    assert np.array_equal(ours, np.asarray(rec.probs, dtype=np.float32))


@pytest.mark.slow
def test_two_runs_identical_npy(tmp_path):
    src = noise_wav(tmp_path / "a.wav", 5)
    a = compute_probs(src, str(tmp_path / "o1"))
    b = compute_probs(src, str(tmp_path / "o2"))
    assert (a / "probs.npy").read_bytes() == (b / "probs.npy").read_bytes()
    meta = json.loads((a / "probs.json").read_text())
    assert meta["n_frames"] == len(np.load(a / "probs.npy")) == n_frames(5 * SR)
    assert meta["source_n_samples"] == 5 * SR and meta["torch_threads"] == 1
    assert meta["ffmpeg_filter"] == "aresample=16000:resampler=swr"
    assert not (tmp_path / "o1" / (a.name + ".tmp")).exists()


@pytest.mark.slow
def test_existing_dir_reused_and_orphan_tmp_removed(tmp_path):
    src = noise_wav(tmp_path / "a.wav", 1)
    out = tmp_path / "o"
    orphan = out / (probs_dir_name(src, sha256_file(src), None) + ".tmp")
    orphan.mkdir(parents=True)
    (out / "backup.tmp").mkdir()  # чужая папка владельца в --out
    (orphan / "junk").write_text("x")
    dst = compute_probs(src, str(out))
    assert (out / "backup.tmp").exists()
    assert not orphan.exists() and sorted(p.name for p in dst.iterdir()) == ["probs.json",
                                                                              "probs.npy"]
    stamp = (dst / "probs.npy").stat().st_mtime_ns
    assert compute_probs(src, str(out)) == dst  # переиспользование (D19), без пересчёта
    assert (dst / "probs.npy").stat().st_mtime_ns == stamp


@pytest.mark.slow
def test_ffmpeg_failure_publishes_nothing(tmp_path, monkeypatch):
    src = noise_wav(tmp_path / "a.wav", 1)
    monkeypatch.setattr("voxcut.audio.vad_filter", lambda channel, channels: "bogusfilter")
    with pytest.raises(FFmpegError):
        compute_probs(src, str(tmp_path / "o"))
    # .tmp остаётся до следующего запуска, итогового каталога нет
    assert [p.name for p in (tmp_path / "o").iterdir()] == [
        probs_dir_name(src, sha256_file(src), None) + ".tmp"]


@pytest.mark.slow
def test_rss_bounded_over_50k_frames(tmp_path, peak_growth_mb):
    """C1: without inference_mode RSS grows ~21 KB/frame (~1 GB here)."""
    warm = noise_wav(tmp_path / "warm.wav", 2)
    src = str(tmp_path / "long.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    f"anoisesrc=d={50_000 * 512 / SR}:r={SR}:a=0.3", "-c:a", "pcm_s16le", src],
                   check=True)
    grew_mb = peak_growth_mb(
        "from silero_vad import load_silero_vad\n"
        "from voxcut.audio import vad_filter, vad_probs\n"
        f"m, af = load_silero_vad(), vad_filter(None, 1)\nvad_probs({warm!r}, af, m)",
        f"assert len(vad_probs({src!r}, af, m)[0]) == 50_000")
    assert grew_mb < 50, f"RSS вырос на {grew_mb:.0f} МБ"
