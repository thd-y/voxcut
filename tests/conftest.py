import importlib.util
import shutil
import subprocess
import sys

import pytest


def pytest_collection_modifyitems(config, items):
    # D9: slow-тесты пропускаются, если нет ffmpeg или torch, а не падают.
    missing = [name for name, ok in (
        ("ffmpeg", shutil.which("ffmpeg") is not None),
        ("torch", importlib.util.find_spec("torch") is not None),
    ) if not ok]
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"нет: {', '.join(missing)}")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def peak_growth_mb():
    """Peak RSS growth of `action` in a fresh interpreter, so earlier tests cannot hide it.

    VmHWM, not ru_maxrss: Linux carries ru_maxrss of the parent (pytest with torch) across exec.
    """

    def run(setup: str, action: str) -> float:
        hwm = ("lambda: int(next(ln for ln in open('/proc/self/status') "
               "if ln.startswith('VmHWM')).split()[1])")
        code = f"hwm = {hwm}\n{setup}\nbefore = hwm()\n{action}\nprint((hwm() - before) / 1024)"
        r = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True,
                           text=True)
        return float(r.stdout.split()[-1])

    return run
