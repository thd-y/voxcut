import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from voxcut.audio import (
    ENV_KEYS,
    FFmpegError,
    RunError,
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
def test_reset_at_seam_equals_separate_files(tmp_path, model):
    """D29: after a frame-aligned seam the probs equal a run over the second file alone."""
    a, b = noise_wav(tmp_path / "a.wav", 1.024), noise_wav(tmp_path / "b.wav", 1.024, seed=1)
    x = np.concatenate([sf.read(a, dtype="int16")[0], sf.read(b, dtype="int16")[0]])
    ab = tmp_path / "ab.wav"
    sf.write(ab, x, SR, subtype="PCM_16")
    af = vad_filter(None, 1)
    sep = np.concatenate([vad_probs(a, af, model)[0], vad_probs(b, af, model)[0]])
    assert np.array_equal(vad_probs(str(ab), af, model, reset_at=[32])[0], sep)
    assert not np.array_equal(vad_probs(str(ab), af, model)[0], sep)  # без сброса иначе


@pytest.mark.slow
def test_two_pass_is_max(tmp_path, model):
    """D31: the second pass resets every reset_period frames; probs are the elementwise max."""
    src, af = noise_wav(tmp_path / "a.wav", 5), vad_filter(None, 1)
    a = vad_probs(src, af, model)[0]
    two = vad_probs(src, af, model, reset_period=32)[0]
    assert two.dtype == np.float32 and len(two) == len(a) == 157
    assert np.array_equal(two, np.maximum(a, vad_probs(src, af, model, reset_at=range(32, 157, 32))[0]))
    assert not np.array_equal(two, a)  # второй проход что-то добавил


@pytest.mark.slow
def test_two_pass_short_source_unchanged(tmp_path, model):
    """D31: shorter than one period, pass B never resets differently, so the max is pass A."""
    src, af = noise_wav(tmp_path / "a.wav", 5), vad_filter(None, 1)
    assert np.array_equal(vad_probs(src, af, model, reset_period=3750)[0], vad_probs(src, af, model)[0])


@pytest.mark.slow
def test_two_pass_respects_seams(tmp_path, model):
    """D31 + D29: pass A resets at seams only, pass B at seams and every period."""
    src, af = noise_wav(tmp_path / "a.wav", 5), vad_filter(None, 1)
    two = vad_probs(src, af, model, reset_at=[40], reset_period=32)[0]
    assert np.array_equal(two, np.maximum(vad_probs(src, af, model, reset_at=[40])[0],
                                          vad_probs(src, af, model, reset_at=[32, 40, 64, 96, 128])[0]))


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
    # регрессия D25: без session.json ни session, ни channel_source в probs.json
    assert set(meta) == {*ENV_KEYS, "source_path", "source_sha256", "source_sr",
                         "source_n_samples", "channels", "channel", "codec_name", "format",
                         "subtype", "vad_sr", "frame_samples", "n16k", "n_frames", "model_name",
                         "torch_version", "reset_period_frames"}
    assert meta["reset_period_frames"] == 3750  # D31


@pytest.mark.slow
def test_existing_dir_reused_and_orphan_tmp_removed(tmp_path):
    src = noise_wav(tmp_path / "a.wav", 1)
    out = tmp_path / "o"
    orphan = out / (probs_dir_name(src, sha256_file(src), None) + ".tmp")
    orphan.mkdir(parents=True)
    (out / "backup.tmp").mkdir()  # a foreign folder in --out
    (orphan / "junk").write_text("x")
    dst = compute_probs(src, str(out))
    assert (out / "backup.tmp").exists()
    assert not orphan.exists() and sorted(p.name for p in dst.iterdir()) == ["probs.json",
                                                                              "probs.npy"]
    stamp = (dst / "probs.npy").stat().st_mtime_ns
    assert compute_probs(src, str(out)) == dst  # переиспользование (D19), без пересчёта
    assert (dst / "probs.npy").stat().st_mtime_ns == stamp


@pytest.mark.slow
@pytest.mark.parametrize("period", [None, 1875])
def test_probs_without_period_refused_on_reuse(tmp_path, period):
    """D31: a probs dir without the second pass, or with another period, is not reused silently."""
    src = noise_wav(tmp_path / "a.wav", 1)
    dst = compute_probs(src, str(tmp_path / "o"))
    meta = json.loads((dst / "probs.json").read_text())
    meta["reset_period_frames"] = period
    if period is None:
        del meta["reset_period_frames"]
    (dst / "probs.json").write_text(json.dumps(meta))
    with pytest.raises(RunError, match="(?s)D31.*rm -rf") as e:
        compute_probs(src, str(tmp_path / "o"))
    assert e.value.code == "run_no_reset_period"


@pytest.mark.slow
def test_ffmpeg_failure_publishes_nothing(tmp_path, monkeypatch):
    src = noise_wav(tmp_path / "a.wav", 1)
    monkeypatch.setattr("voxcut.audio.vad_filter", lambda channel, channels: "bogusfilter")
    with pytest.raises(FFmpegError) as e:
        compute_probs(src, str(tmp_path / "o"))
    assert e.value.code == "ffmpeg_failed"
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
        f"assert len(vad_probs({src!r}, af, m, reset_period=3750)[0]) == 50_000")
    assert grew_mb < 50, f"RSS вырос на {grew_mb:.0f} МБ"  # D31: и со второй моделью
