import os
import subprocess

import pytest

from voxcut.audio import FFmpegError, ffmpeg_stream, find_ffmpeg


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """Put a shell-script ffmpeg first in PATH; `body` handles every call except -version."""

    def install(body: str):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "ffmpeg"
        script.write_text(
            '#!/bin/sh\n[ "$2" = "-version" ] && { echo "ffmpeg version fake"; exit 0; }\n' + body
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    return install


def consume_and_publish(tmp_path, args):
    """Caller pattern from D6: write to .tmp, publish with os.replace only after the stream ends."""
    tmp, final = tmp_path / "out.tmp", tmp_path / "out"
    with ffmpeg_stream(args) as stdout, open(tmp, "wb") as f:
        f.write(stdout.read())
    os.replace(tmp, final)
    return final


def test_missing_ffmpeg_message(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    msg = "ffmpeg не найден, установите: sudo apt install ffmpeg"
    with pytest.raises(FFmpegError, match=msg):
        find_ffmpeg()
    with pytest.raises(FFmpegError, match=msg), ffmpeg_stream(["-i", "x"]):
        pass


def test_version_line(fake_ffmpeg):
    fake_ffmpeg("exit 0\n")
    assert find_ffmpeg()[1] == "ffmpeg version fake"


@pytest.mark.parametrize("body", [
    "head -c 100000 /dev/zero; exit 1\n",  # падение после частичного вывода
    "head -c 100000 /dev/zero; echo 'Decoding error' >&2; exit 0\n",  # ошибка декода при коде 0
])
def test_failure_after_partial_output_publishes_nothing(tmp_path, fake_ffmpeg, body):
    fake_ffmpeg(body)
    with pytest.raises(FFmpegError):
        consume_and_publish(tmp_path, ["-i", "x"])
    assert not (tmp_path / "out").exists()


def test_args_passed_as_list_without_shell(tmp_path, fake_ffmpeg):
    fake_ffmpeg('printf "%s|" "$@"\n')
    out = consume_and_publish(tmp_path, ["-i", "a b; rm -rf x"])
    assert out.read_bytes().decode().endswith("-i|a b; rm -rf x|")


@pytest.mark.slow
def test_real_ffmpeg_decode_error_is_refusal(tmp_path):
    src = tmp_path / "s.flac"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=d=3",
                    "-c:a", "flac", str(src)], check=True)
    assert consume_and_publish(tmp_path, ["-i", str(src), "-f", "s16le", "-"]).stat().st_size \
        == 3 * 44100 * 2
    (tmp_path / "out").unlink()
    src.write_bytes(src.read_bytes()[:30000])  # обрезанный FLAC: ffmpeg 6.1 выходит с кодом 0
    with pytest.raises(FFmpegError, match="Decoding error|decode_frame"):
        consume_and_publish(tmp_path, ["-i", str(src), "-f", "s16le", "-"])
    assert not (tmp_path / "out").exists()
