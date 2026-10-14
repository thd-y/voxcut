import json
import resource
import subprocess

import numpy as np
import pytest
import soundfile as sf

from voxcut.audio import RunError, export_clips, sha256_file
from voxcut.cli import main

SR = 48000
DTYPE = {"PCM_16": "int16", "PCM_24": "int32", "PCM_32": "int32", "FLOAT": "float32"}


def make_source(path, subtype, fmt="WAV", seconds=2, channels=2, seed=0):
    rng = np.random.default_rng(seed)
    sf.write(path, rng.uniform(-0.9, 0.9, (SR * seconds, channels)), SR, subtype=subtype,
             format=fmt)
    return str(path)


def make_seg_dir(root, src, intervals):
    """seg-dir as `segment` leaves it: run.json + manifest.jsonl."""
    d = root / "seg-test"
    d.mkdir()
    sha = sha256_file(src)
    run = {"source": {"path": src, "sha256": sha, "sr": SR, "n_samples": sf.info(src).frames}}
    (d / "run.json").write_text(json.dumps(run))
    rows = [{"source_sha256": sha, "start_sample": s, "end_sample": e, "audio_path": None}
            for s, e in intervals]
    (d / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d


@pytest.mark.parametrize("subtype, fmt", [
    ("PCM_16", "WAV"), ("PCM_24", "WAV"), ("PCM_32", "WAV"), ("FLOAT", "WAV"),
    ("PCM_24", "FLAC"), ("PCM_16", "FLAC"),
])
def test_clips_bit_exact(tmp_path, subtype, fmt):
    src = make_source(tmp_path / f"s.{fmt.lower()}", subtype, fmt)
    clips = export_clips(make_seg_dir(tmp_path, src, [(0, 1000), (12345, 54321),
                                                      (SR * 2 - 7, SR * 2)]))
    rows = [json.loads(x) for x in (clips / "manifest.jsonl").read_text().splitlines()]
    whole, _ = sf.read(src, dtype=DTYPE[subtype], always_2d=True)
    for r in rows:
        clip = clips.parent / r["audio_path"]
        info = sf.info(clip)
        assert (info.subtype, info.format, info.channels) == (subtype, fmt, 2)
        got, _ = sf.read(clip, dtype=DTYPE[subtype], always_2d=True)
        assert np.array_equal(got, whole[r["start_sample"]:r["end_sample"]])


def test_end_clamped_to_eof(tmp_path):
    src = make_source(tmp_path / "s.wav", "PCM_16")
    clips = export_clips(make_seg_dir(tmp_path, src, [(SR * 2 - 10, SR * 2 + 500)]))
    row = json.loads((clips / "manifest.jsonl").read_text())
    assert row["end_sample"] == SR * 2 and sf.info(clips.parent / row["audio_path"]).frames == 10


def test_existing_clips_refused(tmp_path):
    src = make_source(tmp_path / "s.wav", "PCM_16")
    seg = make_seg_dir(tmp_path, src, [(0, 100)])
    export_clips(seg)
    with pytest.raises(RunError, match="уже существует"):
        export_clips(seg)


def test_changed_source_refused(tmp_path):
    src = make_source(tmp_path / "s.wav", "PCM_16")
    seg = make_seg_dir(tmp_path, src, [(0, 100)])
    make_source(tmp_path / "s.wav", "PCM_16", seed=1)
    with pytest.raises(RunError, match="sha256"):
        export_clips(seg)
    assert not (seg / "clips").exists()


def test_orphan_tmp_removed_and_zero_intervals(tmp_path):
    src = make_source(tmp_path / "s.wav", "PCM_16")
    seg = make_seg_dir(tmp_path, src, [])
    (seg / "clips.tmp").mkdir()
    (seg / "clips.tmp" / "junk.wav").write_text("x")
    clips = export_clips(seg)
    assert [p.name for p in clips.iterdir()] == ["manifest.jsonl"]


def test_cli_export(tmp_path, capsys):
    src = make_source(tmp_path / "s.wav", "PCM_16")
    seg = make_seg_dir(tmp_path, src, [(0, 100)])
    assert main(["export", str(seg)]) == 0
    assert capsys.readouterr().out.strip() == str(seg / "clips")


@pytest.mark.slow
def test_whole_file_interval_rss_bounded(tmp_path):
    """C7: a clip as long as the source must not be read into memory at once."""
    src = str(tmp_path / "long.wav")
    seconds = 300  # 48k стерео FLOAT: 115 МБ на диске, 230 МБ в float64 при read() целиком
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    f"anoisesrc=d={seconds}:r={SR}:a=0.3", "-ac", "2", "-c:a", "pcm_f32le", src],
                   check=True)
    seg = make_seg_dir(tmp_path, src, [(0, SR * seconds)])
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    clips = export_clips(seg)
    grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    row = json.loads((clips / "manifest.jsonl").read_text())
    assert sf.info(clips.parent / row["audio_path"]).frames == SR * seconds
    assert grew_mb < 50, f"RSS вырос на {grew_mb:.0f} МБ"
