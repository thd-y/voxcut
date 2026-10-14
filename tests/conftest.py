import importlib.util
import shutil

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
