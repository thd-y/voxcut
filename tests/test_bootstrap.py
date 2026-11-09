import os
import subprocess
from pathlib import Path

import pytest

from voxcut import bootstrap, runtime


class Executed(Exception):
    pass


@pytest.fixture
def boot(tmp_path, monkeypatch):
    root = tmp_path / 'Продукт [test] & data'
    root.mkdir()
    monkeypatch.setattr(runtime, 'BASE', tmp_path / 'runtime')
    monkeypatch.setattr(bootstrap.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(bootstrap.ctypes.util, 'find_library', lambda name: 'libsndfile.so.1')
    monkeypatch.chdir(tmp_path)
    state = {'ready': False, 'setup_rc': 0, 'calls': [], 'exec': []}

    def run(argv, **kw):
        state['calls'].append((argv, kw))
        if argv == ['make', 'setup']:
            if state['setup_rc'] == 0:
                python = root / '.venv/bin/python'
                python.parent.mkdir(parents=True, exist_ok=True)
                python.touch()
                state['ready'] = True
            return subprocess.CompletedProcess(argv, state['setup_rc'])
        if argv[0].endswith('/python'):
            return subprocess.CompletedProcess(argv, 0 if state['ready'] else 1)
        return subprocess.CompletedProcess(argv, 0)

    def execute(program, argv):
        state['exec'].append((program, argv, Path.cwd()))
        raise Executed

    monkeypatch.setattr(bootstrap.subprocess, 'run', run)
    monkeypatch.setattr(bootstrap.os, 'execv', execute)
    return root, state


def test_first_launch_prepares_own_environment_and_inherits_proxy(boot, monkeypatch):
    root, state = boot
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:12345')
    with pytest.raises(Executed):
        bootstrap.main(root)
    assert [a for a, kw in state['calls']].count(['make', 'setup']) == 1
    assert all('env' not in kw and not kw.get('shell') for a, kw in state['calls'])
    assert os.environ['HTTPS_PROXY'] == 'http://127.0.0.1:12345'
    python = str(root / '.venv/bin/python')
    assert state['exec'] == [(python, [python, '-m', 'voxcut.ui.launch', '--root', str(root)], root)]
    assert (root / '.venv/.voxcut-root').read_text() == str(root)
    assert not (runtime.runtime_dir(root) / 'data').exists()


@pytest.mark.parametrize('moved,incomplete', [(False, False), (True, False), (False, True)])
def test_existing_environment_is_reused_or_repaired(boot, moved, incomplete):
    root, state = boot
    python = root / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    (root / '.venv/.voxcut-root').write_text('/previous/location' if moved else str(root))
    state['ready'] = not incomplete
    with pytest.raises(Executed):
        bootstrap.main(root)
    setup_count = sum(a == ['make', 'setup'] for a, kw in state['calls'])
    assert setup_count == int(moved or incomplete)


def test_setup_failure_never_starts_ui_or_marks_ready(boot):
    root, state = boot
    state['setup_rc'] = 7
    assert bootstrap.main(root) == 7
    assert state['exec'] == []
    assert not (root / '.venv/.voxcut-root').exists()


def test_missing_system_package_explains_prerequisites(boot, monkeypatch, capsys):
    root, state = boot
    monkeypatch.setattr(bootstrap.shutil, 'which', lambda name: None if name == 'ffmpeg' else name)
    assert bootstrap.main(root) != 0
    assert 'ffmpeg' in capsys.readouterr().err
    assert state['exec'] == []
    assert not any(a == ['make', 'setup'] for a, kw in state['calls'])


def test_unexecutable_environment_is_repaired(boot, monkeypatch):
    root, state = boot
    python = root / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    real_run = bootstrap.subprocess.run

    def run(argv, **kw):
        if argv[0] == str(python) and not state['ready']:
            raise OSError('broken interpreter')
        return real_run(argv, **kw)

    monkeypatch.setattr(bootstrap.subprocess, 'run', run)
    with pytest.raises(Executed):
        bootstrap.main(root)
    assert any(a == ['make', 'setup'] for a, kw in state['calls'])


def test_unavailable_systemd_stops_before_setup(boot, monkeypatch, capsys):
    root, state = boot
    monkeypatch.setattr(bootstrap.subprocess, 'run',
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1))
    assert bootstrap.main(root) != 0
    assert 'systemd' in capsys.readouterr().err
    assert state['exec'] == []


def test_two_first_launches_prepare_environment_once(tmp_path):
    import sys

    root = tmp_path / 'product'
    root.mkdir()
    setup = root / 'prepare.py'
    setup.write_text(
        'from pathlib import Path\nimport time\n'
        'with open("events", "a") as f: f.write("start\\n")\n'
        'time.sleep(0.3)\n'
        'p = Path(".venv/bin/python"); p.parent.mkdir(parents=True)\n'
        'p.write_text("#!/bin/sh\\nexit 0\\n"); p.chmod(0o755)\n'
        'with open("events", "a") as f: f.write("end\\n")\n')
    code = f'''
import sys
from pathlib import Path
from voxcut import bootstrap, runtime
runtime.BASE = Path({str(tmp_path / 'runtime')!r})
bootstrap.shutil.which = lambda name: name
bootstrap.ctypes.util.find_library = lambda name: name
real_run = bootstrap.subprocess.run
def run(argv, **kw):
    if argv[0] == 'systemctl':
        return bootstrap.subprocess.CompletedProcess(argv, 0)
    if argv == ['make', 'setup']:
        argv = [sys.executable, {str(setup)!r}]
    return real_run(argv, **kw)
bootstrap.subprocess.run = run
bootstrap.os.execv = lambda *a: sys.exit(0)
bootstrap.main(Path({str(root)!r}))
'''
    processes = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
    assert (root / 'events').read_text().splitlines() == ['start', 'end']
