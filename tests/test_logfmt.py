from datetime import datetime

import pytest

from voxcut import logfmt

W = datetime(2026, 10, 27, 9, 31, 12)


@pytest.mark.parametrize("level,msg,want", [
    ("INFO", "stage=probs", "27.10.26 09:31 INFO  stage=probs"),
    ("WARNING", "x", "27.10.26 09:31 WARNING x"),
    ("ERROR", "a\n  b\nc", "27.10.26 09:31 ERROR a\n  b\nc"),
])
def test_line(level, msg, want):
    assert logfmt.line(level, msg, W) == want


@pytest.mark.parametrize("s,want", [(0, "0s"), (12.7, "12s"), (60, "1m00s"), (2045, "34m05s"),
                                    (3725, "1h02m05s")])
def test_duration(s, want):
    assert logfmt.duration(s) == want
