"""Everything that touches ffmpeg, soundfile or the model (D1)."""

import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager


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
