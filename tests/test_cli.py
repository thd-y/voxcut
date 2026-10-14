import json
import shutil

import numpy as np
import pytest
import soundfile as sf

from voxcut.audio import (
    ENV_KEYS,
    RunError,
    compute_probs,
    current_env,
    probs_dir_name,
    sha256_file,
)
from voxcut.cli import FW_NAME, evaluate, main, segment
from voxcut.intervals import FRAME_SAMPLES as F
from voxcut.intervals import parse_config

SR = 48000
ENV = {"silero_vad_version": "6.2.3", "model_version": "6.2.3", "ffmpeg_version": "ffmpeg x",
       "ffmpeg_filter": "aresample=16000:resampler=swr", "torch_threads": 1}


def make_probs_dir(root, probs, name="rec.abcdef01.raw", sha="ab" * 32, channel=None):
    """A published probs dir as `probs` would leave it, without running the model."""
    d = root / name
    d.mkdir(parents=True)
    probs = np.asarray(probs, dtype=np.float32)
    np.save(d / "probs.npy", probs)
    n16k = len(probs) * F
    meta = ENV | {"source_path": "/x/rec.wav", "source_sha256": sha, "source_sr": SR,
                  "source_n_samples": n16k * SR // 16000, "channels": 1, "channel": channel,
                  "codec_name": "pcm_s16le", "n16k": n16k, "n_frames": len(probs)}
    (d / "probs.json").write_text(json.dumps(meta))
    return d


def cfg(text="threshold = 0.5\npad_ms = 0\nmerge_gap_ms = 0\n"):
    return parse_config(text)[0]


# речь 1–2 с и 5–6 с (кадр 32 мс), всего 10 с
SPEECH = np.full(313, 0.05)
SPEECH[31:63] = 0.9
SPEECH[156:188] = 0.9


def test_segment_writes_manifest_in_source_rate(tmp_path):
    seg = segment(make_probs_dir(tmp_path, SPEECH), cfg())
    rows = [json.loads(x) for x in (seg / "manifest.jsonl").read_text().splitlines()]
    assert [(r["start_sample"], r["end_sample"]) for r in rows] == \
        [(31 * F * 3, 63 * F * 3), (156 * F * 3, 188 * F * 3)]
    r = rows[0]
    assert r["branch"] == "raw" and r["audio_path"] is None and r["source_sr"] == SR
    assert r["neg_threshold"] == pytest.approx(0.35) and r["p_max"] == pytest.approx(0.9)
    run = json.loads((seg / "run.json").read_text())
    assert run["counts"] == {"raw": 2, "final": 2} and set(run["env"]) == set(ENV_KEYS)


def test_zero_detections_still_writes_run_json(tmp_path):
    seg = segment(make_probs_dir(tmp_path, [0.01] * 50), cfg())
    assert json.loads((seg / "run.json").read_text())["counts"] == {"raw": 0, "final": 0}
    assert (seg / "manifest.jsonl").read_text() == ""


def test_same_config_reused_in_any_key_order(tmp_path):
    d = make_probs_dir(tmp_path, SPEECH)
    a = segment(d, cfg())
    stamp = (a / "run.json").stat().st_mtime_ns
    b = segment(d, cfg("merge_gap_ms = 0\nthreshold = 0.5\npad_ms = 0\n"))
    assert a == b and (b / "run.json").stat().st_mtime_ns == stamp
    assert segment(d, cfg("threshold = 0.4\npad_ms = 0\nmerge_gap_ms = 0\n")) != a


def test_replaced_probs_refused(tmp_path):
    d = make_probs_dir(tmp_path, SPEECH)
    segment(d, cfg())
    np.save(d / "probs.npy", np.asarray(SPEECH[::-1], dtype=np.float32))
    with pytest.raises(RunError, match="подменён"):
        segment(d, cfg())


@pytest.mark.parametrize("damage, needle", [
    (lambda d: (d / "probs.json").unlink(), "нет .*probs.json"),
    (lambda d: np.save(d / "probs.npy", np.zeros(5, dtype=np.float32)), "5 кадров"),
])
def test_incomplete_probs_dir_refused(tmp_path, damage, needle):
    d = make_probs_dir(tmp_path, SPEECH)
    damage(d)
    with pytest.raises(RunError, match=needle):
        segment(d, cfg())


def test_only_own_orphan_tmp_removed(tmp_path):
    d = make_probs_dir(tmp_path, SPEECH)
    own = d / f"seg-{cfg().sha256()[:8]}.tmp"
    own.mkdir()
    (own / "junk").write_text("x")
    (d / "seg-deadbeef.tmp").mkdir()  # параллельный прогон другого конфига
    seg = segment(d, cfg())
    assert not own.exists() and not (seg / "junk").exists()
    assert (d / "seg-deadbeef.tmp").exists()


def test_channel_mismatch_refused(tmp_path):
    d = make_probs_dir(tmp_path, SPEECH, channel=0)
    segment(d, cfg())  # без channel в конфиге: probs уже определяют канал
    with pytest.raises(RunError, match="channel"):
        segment(d, cfg("threshold = 0.5\npad_ms = 0\nmerge_gap_ms = 0\nchannel = 1\n"))


LABELS = "0\t10\tscored\n1.0\t2.0\tme\n5.0\t6.0\tme\n8.0\t9.0\tme\n"


def test_evaluate(tmp_path):
    (tmp_path / "l.txt").write_text(LABELS)
    m, ref = evaluate(segment(make_probs_dir(tmp_path, SPEECH), cfg()), tmp_path / "l.txt")
    assert ref == {"me_events": 3, "me_sec": 3.0, "scored_sec": 10.0}
    assert m.fully_missed == [(8.0, 9.0)] and m.recall_events == pytest.approx(2 / 3)


def test_evaluate_refuses_labels_past_source_end(tmp_path):
    (tmp_path / "l.txt").write_text("0\t20\tscored\n")
    with pytest.raises(Exception, match="длительности источника"):
        evaluate(segment(make_probs_dir(tmp_path, SPEECH), cfg()), tmp_path / "l.txt")


def test_sweep_table(tmp_path, capsys):
    a = make_probs_dir(tmp_path, SPEECH, name="a.11111111.raw", sha="11" * 32)
    b = make_probs_dir(tmp_path, [0.01] * 313, name="b.22222222.raw", sha="22" * 32)
    (tmp_path / "la.txt").write_text(LABELS)
    (tmp_path / "lb.txt").write_text("0\t10\tscored\n")
    c1, c2 = tmp_path / "t5.toml", tmp_path / "t3.toml"
    c1.write_text('threshold = 0.5\npad_ms = 0\nmerge_gap_ms = 0\n[meta]\nname = "t5"\n')
    c2.write_text("threshold = 0.3\npad_ms = 250\nmerge_gap_ms = 300\n")
    assert main(["sweep", str(c1), str(c2), "--session", str(a), str(tmp_path / "la.txt"),
                 "--session", str(b), str(tmp_path / "lb.txt")]) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = [ln.split("\t") for ln in lines[1:-1]]
    assert [(r[0], r[1]) for r in rows] == [
        (n, s) for n in ("t5", "t3", FW_NAME) for s in (a.name, b.name, "ALL")]
    t5 = {r[1]: r for r in rows if r[0] == "t5"}
    assert t5[b.name][2] == "n/a" and t5["ALL"][2] == t5[a.name][2] == "0.667"
    assert "параметры FW, постобработка VoxCut" in lines[-1]


def test_main_prints_refusal_without_traceback(tmp_path, capsys):
    assert main(["segment", str(tmp_path / "nope"), "--config", str(tmp_path / "c.toml")]) == 2
    assert capsys.readouterr().err.startswith("voxcut: ")


# --- probs reuse and env drift (D19) ---

@pytest.mark.slow
def test_probs_env_drift_refused_with_rm_command(tmp_path):
    src = tmp_path / "rec.wav"
    sf.write(src, np.zeros(16000), 16000)
    out = tmp_path / "out"
    d = make_probs_dir(out, [0.0] * 32, name=probs_dir_name(str(src), sha256_file(str(src)),
                                                              None))
    meta = json.loads((d / "probs.json").read_text())
    meta |= current_env("aresample=16000:resampler=swr")
    (d / "probs.json").write_text(json.dumps(meta))
    assert compute_probs(str(src), str(out)) == d  # переиспользован, модель не запускалась
    (d / "probs.json").write_text(json.dumps(meta | {"ffmpeg_version": "ffmpeg 5.0"}))
    with pytest.raises(RunError, match=f"(?s)ffmpeg_version.*rm -rf {d}"):
        compute_probs(str(src), str(out))


@pytest.mark.slow
def test_detect_and_evaluate_end_to_end(tmp_path, capsys):
    """Smoke: real source -> detect -> evaluate through the CLI."""
    rng = np.random.default_rng(0)
    src = tmp_path / "rec.wav"
    sf.write(src, rng.uniform(-0.2, 0.2, 44100 * 4), 44100, subtype="PCM_24")
    (tmp_path / "c.toml").write_text("threshold = 0.1\npad_ms = 100\nmerge_gap_ms = 300\n")
    assert main(["detect", str(src), "--config", str(tmp_path / "c.toml"),
                 "--out", str(tmp_path / "out")]) == 0
    seg = tmp_path / "out" / capsys.readouterr().out.strip().split("/out/")[1]
    run = json.loads((seg / "run.json").read_text())
    assert run["source"]["n_samples"] == 44100 * 4 and run["source"]["codec"] == "pcm_s24le"
    (tmp_path / "l.txt").write_text("0\t4\tscored\n")
    assert main(["evaluate", str(seg), str(tmp_path / "l.txt")]) == 0
    assert "эталон: событий me 0" in capsys.readouterr().out
    shutil.rmtree(tmp_path / "out")
