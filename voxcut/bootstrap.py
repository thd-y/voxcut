"""Prepare this product's Python environment, then replace the launcher with its UI."""

import ctypes.util
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

from voxcut.runtime import runtime_dir

ROOT = Path(__file__).resolve().parents[1]
CHECK = ("import voxcut.ui.launch; from importlib.metadata import version; "
         "assert version('torch').endswith('+cpu'); version('silero-vad')")


def main(root: Path = ROOT) -> int:
    if sys.version_info < (3, 12):  # noqa: UP036 - bootstrap runs in the system Python
        print('VoxCut requires Python 3.12 or newer in WSL. See docs/running.md.', file=sys.stderr)
        return 1
    missing = [name for name in ('make', 'ffmpeg', 'systemctl') if not shutil.which(name)]
    if not ctypes.util.find_library('sndfile'):
        missing.append('libsndfile1')
    if missing:
        print('Missing WSL prerequisites: ' + ', '.join(missing)
              + '. See docs/running.md.', file=sys.stderr)
        return 1
    if subprocess.run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, check=False).returncode:
        print('WSL systemd user services are unavailable. See docs/running.md.', file=sys.stderr)
        return 1
    root = root.resolve()
    python = root / '.venv' / 'bin' / 'python'
    stamp = root / '.venv' / '.voxcut-root'
    with (runtime_dir(root) / 'setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ready = python.is_file() and subprocess.run(
                [str(python), '-c', CHECK], cwd=root, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False).returncode == 0
        except OSError:
            ready = False
        moved = stamp.exists() and stamp.read_text() != str(root)
        if not ready or moved:
            print('Preparing the local Python environment (CPU torch); proxy settings are '
                  'inherited. This may take a few minutes.', flush=True)
            result = subprocess.run(['make', 'setup'], cwd=root, check=False)
            if result.returncode:
                print('Environment setup failed. See docs/running.md; keep your proxy enabled.',
                      file=sys.stderr)
                return result.returncode
        stamp.write_text(str(root))
    os.chdir(root)
    os.execv(str(python), [str(python), '-m', 'voxcut.ui.launch', '--root', str(root)])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f'VoxCut could not start: {exc}. See docs/running.md.', file=sys.stderr)
        raise SystemExit(1) from None
