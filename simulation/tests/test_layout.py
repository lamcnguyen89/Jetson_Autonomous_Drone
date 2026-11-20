"""Regression checks for running simulation tools outside the project root."""

import subprocess
import sys

import pytest

from simulation import paths


def test_default_assets_do_not_depend_on_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('ARDUPILOT_SITL_BINARY', raising=False)
    monkeypatch.setattr(paths.platform, 'system', lambda: 'Linux')

    assert paths.DEFAULTS_FILE.is_file()
    assert paths.sitl_binary_path() == str(paths.ARDUPILOT_ROOT / 'arducopter-linux')


def test_binary_override_is_preserved(monkeypatch, tmp_path):
    executable = tmp_path / 'custom-arducopter'
    monkeypatch.setenv('ARDUPILOT_SITL_BINARY', str(executable))

    assert paths.sitl_binary_path() == str(executable)


def test_linux_rejects_macos_binaries(monkeypatch, tmp_path):
    monkeypatch.delenv('ARDUPILOT_SITL_BINARY', raising=False)
    monkeypatch.setattr(paths.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(paths, 'ARDUPILOT_ROOT', tmp_path)
    (tmp_path / 'arducopter').write_bytes(b'\xcf\xfa\xed\xfe')

    with pytest.raises(FileNotFoundError, match='No usable ArduPilot SITL binary'):
        paths.sitl_binary_path()


def test_runner_help_works_outside_project(tmp_path):
    result = subprocess.run(
        [sys.executable, str(paths.SIMULATION_ROOT / 'sitl.py'), '--help'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    assert '--verbose' in result.stdout


def test_importing_runner_does_not_start_sitl():
    subprocess.run(
        [sys.executable, '-c', 'import simulation.sitl'],
        cwd=paths.SIMULATION_ROOT.parent, timeout=10, check=True,
    )
