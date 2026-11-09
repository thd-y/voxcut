"""Run the batch parser in real cmd.exe; replace only the external WSL process."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def cmd():
    command = shutil.which('cmd.exe')
    if not command or not shutil.which('wslpath'):
        pytest.skip('Windows/WSL interop is unavailable')
    probe = subprocess.run([command, '/d', '/c', 'exit 0'], capture_output=True, timeout=10, check=False)
    if probe.returncode:
        pytest.skip('Windows/WSL interop cannot start in this environment')
    return command


@pytest.mark.parametrize('folder,expected', [
    ('C:\\Projects\\Продукт [test] & data\\', '--cd "C:\\Projects\\Продукт [test] & data\\."'),
    ('D:\\Other folder\\', '--cd "D:\\Other folder\\."'),
    ('\\\\wsl.localhost\\Test Distro\\home\\user\\My Product\\',
     '-d "Test Distro" --cd "/home/user/My Product/"'),
    ('\\\\wsl$\\Ubuntu\\home\\user\\voxcut\\', '-d "Ubuntu" --cd "/home/user/voxcut/"'),
])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_windows_and_wsl_paths_keep_arguments_and_exit_code(tmp_path, cmd, folder, expected,
                                                          exit_code):
    fake = tmp_path / 'fake-wsl.cmd'
    fake.write_bytes(f'@echo off\r\necho WSL_ARGV %*\r\nexit /b {exit_code}\r\n'.encode())
    win = lambda p: subprocess.check_output(['wslpath', '-w', str(p)], text=True).strip()
    script = (ROOT / 'VoxCut.cmd').read_text().replace('%~dp0', folder)
    script = script.replace('wsl.exe', f'call "{win(fake)}"')
    launcher = tmp_path / 'launcher.cmd'
    launcher.write_bytes(script.replace('\n', '\r\n').encode('utf-8'))
    result = subprocess.run([cmd, '/d', '/q'],
                            input=f'call "{win(launcher)}"\r\n\r\nexit %errorlevel%\r\n'.encode(),
                            capture_output=True, timeout=15, check=False)
    output = result.stdout.decode('utf-8', errors='replace')
    assert result.returncode == exit_code, output
    assert expected in output
    if not folder.startswith('\\\\'):
        assert 'WSL_ARGV --cd' in output
    assert '--exec python3 -m voxcut.bootstrap' in output
    assert output.count('WSL_ARGV') == 1
