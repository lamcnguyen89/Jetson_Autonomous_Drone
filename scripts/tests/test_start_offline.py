"""Exercise offline service setup without touching host services or accounts."""

import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def offline(tmp_path):
    project = tmp_path / 'Drone project % check'
    for name in ('scripts', 'systemd', 'src', '.venv/bin'):
        (project / name).mkdir(parents=True)
    shutil.copy2(ROOT / 'systemd/Auto_Drone.service', project / 'systemd/Auto_Drone.service')
    (project / 'src/main.py').write_text('raise RuntimeError("Do not run during setup")\n')
    (project / '.venv/pyvenv.cfg').touch()
    (project / '.venv/bin/python').symlink_to(sys.executable)
    config = f'SERVICE_USER={getpass.getuser()}\nMAVLINK_PORT=/dev/ttyACM0\n'
    (project / '.env').write_text(config)
    units = tmp_path / 'units'
    units.mkdir()
    competitor = units / 'mavros.service'
    competitor.write_text('existing competing service\n')
    script = project / 'scripts/Start_Auto_Drone_Offline.sh'
    source = (ROOT / 'scripts/Start_Auto_Drone_Offline.sh').read_text()
    source = source.replace('if (( EUID != 0 )); then', 'if false; then')
    script.write_text(source.replace('/etc/systemd/system', str(units)))
    commands = tmp_path / 'bin'
    commands.mkdir()
    calls = tmp_path / 'calls.jsonl'
    stub = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'runuser':
    assert args[:3] == ['-u', os.environ['TEST_USER'], '--']
    sys.exit(subprocess.call(args[3:]))
if name in ('apt-get', 'pip', 'curl', 'wget', 'udevadm', 'usermod'):
    sys.exit('Forbidden offline operation')
if name == 'systemd-analyze' and os.environ.get('TEST_VERIFY_FAIL'):
    sys.exit(1)
if name == 'systemctl':
    assert args == ['daemon-reload'] or (
        args[0] in ('enable', 'restart', 'is-active') and args[-1] == 'Auto_Drone.service')
    if args[0] == 'restart' and os.environ.get('TEST_START_FAIL'):
        sys.exit(1)
'''
    for name in ('runuser', 'systemd-analyze', 'systemctl', 'apt-get', 'pip',
                 'curl', 'wget', 'udevadm', 'usermod'):
        command = commands / name
        command.write_text(stub)
        command.chmod(0o755)
    environment = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'],
                       TEST_CALLS=str(calls), TEST_USER=getpass.getuser())

    def run(*args, **overrides):
        result = subprocess.run(['bash', str(script), *args], cwd=tmp_path,
                                env=dict(environment, **overrides), capture_output=True, text=True)
        recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, recorded

    return project, units, run


def test_installs_and_starts_only_project_service(offline):
    project, units, run = offline
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c[0] == 'systemctl'] == [
        ['systemctl', 'daemon-reload'], ['systemctl', 'enable', 'Auto_Drone.service'],
        ['systemctl', 'restart', 'Auto_Drone.service'],
        ['systemctl', 'is-active', '--quiet', 'Auto_Drone.service'],
    ]
    assert {c[0] for c in calls} == {'runuser', 'systemd-analyze', 'systemctl'}
    unit = (units / 'Auto_Drone.service').read_text()
    assert '@PROJECT_ROOT@' not in unit and '@SERVICE_USER@' not in unit
    assert str(project).replace('%', '%%') in unit
    assert f'User={getpass.getuser()}\n' in unit
    assert (units / 'mavros.service').read_text() == 'existing competing service\n'
    assert not list((project / 'scripts').glob('.offline.*'))


@pytest.mark.parametrize('missing', ['.env', '.venv/bin/python'])
def test_missing_local_setup_leaves_services_untouched(offline, missing):
    project, units, run = offline
    (project / missing).unlink()
    result, calls = run()
    assert result.returncode != 0
    assert not calls
    assert not (units / 'Auto_Drone.service').exists()


def test_invalid_unit_leaves_installed_service_untouched(offline):
    project, units, run = offline
    target = units / 'Auto_Drone.service'
    target.write_text('existing project service\n')
    result, calls = run(TEST_VERIFY_FAIL='1')
    assert result.returncode != 0
    assert target.read_text() == 'existing project service\n'
    assert not any(c[0] == 'systemctl' for c in calls)
    assert not list((project / 'scripts').glob('.offline.*'))


def test_start_failure_is_reported(offline):
    _, _, run = offline
    result, _ = run(TEST_START_FAIL='1')
    assert result.returncode != 0
    assert 'Installed, enabled and started' not in result.stdout


def test_help_does_not_change_services(offline):
    _, _, run = offline
    result, calls = run('--help')
    assert result.returncode == 0
    assert not calls
