import os
import tempfile
from pathlib import Path

import pytest

from voxcut import runtime


@pytest.fixture(autouse=True)
def runtime_base(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='voxcut-runtime-', dir='/tmp') as d:
        monkeypatch.setattr(runtime, 'BASE', Path(d) / 'private')
        yield


def test_private_and_distinct_product_directories(tmp_path):
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    a = runtime.runtime_dir(tmp_path / 'one')
    b = runtime.runtime_dir(tmp_path / 'two')
    assert a != b
    assert runtime.runtime_dir(tmp_path / 'one' / '..' / 'one') == a
    for path in (runtime.BASE, a, b):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('kind', ['symlink', 'public', 'wrong_owner'])
def test_unsafe_runtime_refused(tmp_path, monkeypatch, kind):
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.mkdir(mode=0o700)
        runtime.BASE.symlink_to(target, target_is_directory=True)
    else:
        runtime.BASE.mkdir(mode=0o777 if kind == 'public' else 0o700)
        if kind == 'public':
            runtime.BASE.chmod(0o777)
        else:
            monkeypatch.setattr(runtime.os, 'getuid', lambda: os.stat(runtime.BASE).st_uid + 1)
    with pytest.raises(SystemExit, match='Unsafe runtime directory'):
        runtime.runtime_dir(tmp_path)


def test_unsafe_product_runtime_refused(tmp_path):
    path = runtime.runtime_dir(tmp_path)
    path.rmdir()
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SystemExit, match='Unsafe runtime directory'):
        runtime.runtime_dir(tmp_path)


def test_same_product_with_an_alias_shares_runtime(tmp_path):
    root = tmp_path / 'CaseProduct'
    root.mkdir()
    alias = tmp_path / 'caseproduct'
    if not alias.exists():
        alias.symlink_to(root, target_is_directory=True)
    assert runtime.runtime_dir(root) == runtime.runtime_dir(alias)
