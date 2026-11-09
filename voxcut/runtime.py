"""Private, short-lived IPC on Linux storage, including for products on Windows drives."""

import hashlib
import os
import stat
from pathlib import Path

_USER_RUNTIME = Path('/run/user') / str(os.getuid())
BASE = _USER_RUNTIME / 'voxcut' if _USER_RUNTIME.is_dir() else Path(f'/tmp/voxcut-{os.getuid()}')


def runtime_dir(root: Path) -> Path:
    info = root.resolve().stat()
    identity = f'{info.st_dev}:{info.st_ino}'.encode()
    name = hashlib.sha256(identity).hexdigest()[:24]
    for path in (BASE, BASE / name):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SystemExit(f'Unsafe runtime directory: {path}')
    return BASE / name
