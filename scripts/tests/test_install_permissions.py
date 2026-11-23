"""Exercise installer permissions without changing host accounts or devices."""

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
def installer(tmp_path):
    project = tmp_path / 'Drone project % check'
    (project / 'scripts').mkdir(parents=True)
    (project / 'src').mkdir()
    (project / 'src/main.py').write_text('raise SystemExit(0)\n')
    # Stub the hardware/dependency checks while exercising the actual installer.
    camera = project / 'src/camera'
    camera.mkdir()
    (camera / '__init__.py').write_text('')
    (camera / 'tensorrt_yolo.py').write_text(
        'from types import SimpleNamespace\n'
        'def load_tensorrt(): return SimpleNamespace(__version__="10.7.0")\n')
    (camera / 'jetson.py').write_text('def create_tracker(): pass\n')
    (project / 'scripts/prepare_jetson_model.py').write_text('''
import json, os, sys
from pathlib import Path
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps(['prepare-model', *sys.argv[1:]]) + '\\n')
state = json.loads(Path(os.environ['TEST_STATE']).read_text())
if state.get('model_preparation_fails'):
    raise SystemExit('Simulated model preparation failure')
''')
    (project / 'systemd').mkdir()
    shutil.copy2(ROOT / 'systemd/Auto_Drone.service', project / 'systemd/Auto_Drone.service')
    for name in ('80-auto-drone-oak.rules', 'mavlink_udev.py'):
        shutil.copy2(ROOT / 'scripts' / name, project / 'scripts' / name)
    (project / 'requirements.txt').write_text('')
    (project / 'requirements-jetson.txt').write_text('-r requirements.txt\n')
    units, rules, commands = [tmp_path / name for name in ('units', 'rules', 'bin')]
    for directory in (units, rules, commands):
        directory.mkdir()
    script = project / 'scripts/Install_Auto_Drone.sh'
    # Isolate the privilege boundary. The rest of the actual installer runs.
    text = (ROOT / 'scripts/Install_Auto_Drone.sh').read_text()
    text = text.replace('if (( EUID != 0 )); then', 'if false; then')
    for index, unit_dir in enumerate(('/etc/systemd/system', '/run/systemd/system',
                                     '/usr/local/lib/systemd/system',
                                     '/usr/lib/systemd/system', '/lib/systemd/system')):
        isolated_dir = units if index == 0 else tmp_path / f'unit-path-{index}'
        isolated_dir.mkdir(exist_ok=True)
        text = text.replace(unit_dir, str(isolated_dir))
    text = text.replace('/etc/udev/rules.d/', str(rules) + '/')
    script.write_text(text)
    user = getpass.getuser()
    config = project / '.env'
    config.write_text(f'SERVICE_USER={user}\nBASE_PYTHON="{sys.executable}"\nMAVLINK_PORT=/dev/null\n')
    example = (ROOT / '.env.example').read_text()
    example = example.replace('BASE_PYTHON=/usr/bin/python3', f'BASE_PYTHON="{sys.executable}"')
    example = example.replace('MAVLINK_PORT=/dev/ttyACM0', 'MAVLINK_PORT=/dev/null')
    (project / '.env.example').write_text(example)
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(project / '.venv')], check=True)
    state_file, calls_file = tmp_path / 'state.json', tmp_path / 'calls.jsonl'
    state = {'group_exists': True, 'groups': ['video'], 'writable': True, 'subsystem': 'tty'}
    stub = '''#!/usr/bin/python3
import json, os
from pathlib import Path
import sys
name, args = Path(sys.argv[0]).name, sys.argv[1:]
state_file = Path(os.environ['TEST_STATE'])
state = json.loads(state_file.read_text())
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'id':
    if args[0] == '-nG':
        print(' '.join(state['groups']))
    else:
        os.execv('/usr/bin/id', ['id', *args])
elif name == 'getent':
    sys.exit(0 if state['group_exists'] else 2)
elif name == 'groupadd':
    state['group_exists'] = True
elif name == 'usermod':
    assert args[:2] == ['-aG', 'dialout']
    state['groups'].append('dialout')
elif name == 'apt-cache':
    sys.exit(100)
elif name == 'systemctl':
    if args[0] == 'show':
        print(state.get('unit_load_states', {}).get(args[-1], 'not-found'))
    elif args[0] in ('stop', 'disable') and state.get('service_cleanup_fails') == args[0]:
        sys.exit(1)
elif name == 'udevadm' and args[0] == 'info':
    print('SUBSYSTEM=' + state['subsystem'])
    print('ID_BUS=usb\\nID_VENDOR_ID=1209\\nID_MODEL_ID=5741')
    print('ID_SERIAL_SHORT=controller-123\\nID_USB_INTERFACE_NUM=00')
elif name == 'runuser':
    assert args[:3] == ['-u', os.environ['TEST_USER'], '--']
    command = args[3:]
    if '-m' in command and command[command.index('-m') + 1] == 'pip':
        sys.exit(0)
    if command[-3:] == ['test', '-w', '/dev/null'] and not state['writable']:
        sys.exit(1)
    os.execvpe(command[0], command, os.environ)
state_file.write_text(json.dumps(state))
'''
    for name in ('id', 'getent', 'groupadd', 'usermod', 'apt-get', 'apt-cache',
                 'udevadm', 'runuser', 'systemd-analyze', 'systemctl'):
        tool = commands / name
        tool.write_text(stub)
        tool.chmod(0o755)
    environment = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'],
                       TEST_STATE=str(state_file), TEST_CALLS=str(calls_file),
                       TEST_USER=user, SUDO_USER=user)

    def run(*args, process_env=None, **changes):
        state.update(changes)
        state_file.write_text(json.dumps(state))
        calls_file.unlink(missing_ok=True)
        result = subprocess.run(['bash', str(script), *args], cwd='/',
                                env=dict(environment, **(process_env or {})),
                                capture_output=True, text=True, timeout=30)
        state.update(json.loads(state_file.read_text()))
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        return result, calls

    return run, project, units, rules, state


@pytest.mark.parametrize('group_exists,groups', [
    (True, ['video']), (False, ['video']), (True, ['video', 'dialout']),
])
def test_installs_and_verifies_permissions(installer, group_exists, groups):
    run, project, units, rules, state = installer
    result, calls = run('--start', group_exists=group_exists, groups=groups.copy())
    assert result.returncode == 0, result.stdout + result.stderr
    assert state['groups'] == ['video', 'dialout']
    assert any(c[0] == 'groupadd' for c in calls) == (not group_exists)
    assert any(c[0] == 'usermod' for c in calls) == ('dialout' not in groups)
    assert (rules / '80-auto-drone-oak.rules').is_file()
    assert 'ENV{ID_SERIAL_SHORT}=="controller-123"' in (rules / '99-auto-drone-mavlink.rules').read_text()
    template = (project / 'systemd/Auto_Drone.service').read_text()
    expected_unit = template.replace('@PROJECT_ROOT@', str(project).replace('%', '%%'))
    expected_unit = expected_unit.replace('@SERVICE_USER@', getpass.getuser())
    assert (units / 'Auto_Drone.service').read_text() == expected_unit
    assert (units / 'Auto_Drone.service').stat().st_mode & 0o777 == 0o644
    access_check = next(i for i, c in enumerate(calls) if c[-3:] == ['test', '-w', '/dev/null'])
    enable = next(i for i, c in enumerate(calls) if c[:2] == ['systemctl', 'enable'])
    assert access_check < enable
    assert ['udevadm', 'trigger', '--action=change', '/dev/null'] in calls
    assert ['udevadm', 'settle', '--timeout=30'] in calls
    assert ['systemctl', 'restart', 'Auto_Drone.service'] in calls
    assert not list((project / 'scripts').glob('.install.*'))

    # Re-running is idempotent and does not alter unrelated memberships.
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(c[0] in ('groupadd', 'usermod') for c in calls)
    assert not any(c[:2] == ['systemctl', 'restart'] for c in calls)


def test_denied_permissions_prevent_installing_or_starting_unit(installer):
    run, _, units, _, _ = installer
    result, calls = run('--start', writable=False)
    assert result.returncode != 0
    assert 'still cannot read/write' in result.stderr
    assert not (units / 'Auto_Drone.service').exists()
    assert not any(c[0] == 'systemctl' for c in calls)


def test_non_serial_device_prevents_installing_rules_and_unit(installer):
    run, _, units, rules, _ = installer
    result, calls = run(subsystem='mem')
    assert result.returncode != 0
    assert 'not a tty serial device' in result.stderr
    assert not list(rules.iterdir())
    assert not list(units.iterdir())
    assert not any(c[0] == 'systemctl' for c in calls)


def test_disconnected_controller_fails_before_installing_packages(installer):
    run, project, _, _, _ = installer
    config = project / '.env'
    config.write_text(config.read_text().replace('/dev/null', '/dev/auto-drone-test-missing'))
    result, calls = run()
    assert result.returncode != 0
    assert 'Connect the controller' in result.stderr
    assert not any(c[0] in ('apt-get', 'groupadd', 'usermod', 'systemctl') for c in calls)


@pytest.mark.parametrize('sudo_user', [getpass.getuser(), ''])
def test_missing_env_is_created_from_template_and_owned_by_user(installer, sudo_user):
    run, project, _, _, _ = installer
    config = project / '.env'
    config.unlink()
    expected = (project / '.env.example').read_text().replace(
        'SERVICE_USER=your-linux-username', f'SERVICE_USER={getpass.getuser()}',
    )
    result, _ = run(process_env={'SUDO_USER': sudo_user})
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text() == expected
    assert config.stat().st_uid == os.getuid()
    assert config.stat().st_gid == os.getgid()
    assert 'Created ' in result.stdout
    assert not list(project.glob('.env.??????'))


def test_existing_env_is_not_modified_or_repermissioned(installer):
    run, project, _, _, _ = installer
    config = project / '.env'
    before = config.read_bytes() + b'\n# Operator customization\nSPEED=0.33\n'
    config.write_bytes(before)
    config.chmod(0o600)
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o777 == 0o600
    assert 'Created ' not in result.stdout


def test_missing_env_and_template_fail_before_package_installation(installer):
    run, project, _, _, _ = installer
    (project / '.env').unlink()
    (project / '.env.example').unlink()
    result, calls = run()
    assert result.returncode != 0
    assert 'template' in result.stderr and 'missing' in result.stderr
    assert not (project / '.env').exists()
    assert not calls


def test_existing_non_file_env_is_not_replaced(installer):
    run, project, _, _, _ = installer
    config = project / '.env'
    config.unlink()
    config.symlink_to(project / 'missing-target')
    result, calls = run()
    assert result.returncode != 0
    assert 'refusing to replace' in result.stderr
    assert config.is_symlink()
    assert not (project / 'missing-target').exists()
    assert not calls


@pytest.mark.parametrize('flags', [(), ('--with-jetson',)])
def test_default_installs_both_backends_and_prepares_model_before_service(installer, flags):
    run, project, _, _, _ = installer
    result, calls = run(*flags)
    assert result.returncode == 0, result.stdout + result.stderr
    packages = next(c for c in calls if c[:2] == ['apt-get', 'install'])
    assert 'libnvinfer-bin' in packages and 'python3-libnvinfer' in packages
    assert any(str(project / 'requirements-jetson.txt') in c for c in calls)
    prepare = calls.index(['prepare-model', '--if-needed'])
    enable = calls.index(['systemctl', 'enable', 'Auto_Drone.service'])
    assert prepare < enable
    assert sum(c[0] == 'prepare-model' for c in calls) == 1
    assert not any(c[:2] == ['systemctl', 'restart'] for c in calls)


def test_model_preparation_failure_prevents_service_install_or_restart(installer):
    run, _, units, _, _ = installer
    result, calls = run('--start', model_preparation_fails=True)
    assert result.returncode != 0
    assert 'Simulated model preparation failure' in result.stderr
    assert ['prepare-model', '--if-needed'] in calls
    assert not (units / 'Auto_Drone.service').exists()
    assert not any(c[0] == 'systemctl' for c in calls)


CONFLICTING_UNITS = (
    'compact-vla-clock.service', 'compact-vla-drone.service',
    'compact-vla-server.service', 'luxonis-rlds-recorder.service',
    'mavros-stream-monitor.service', 'mavros.service',
)


def test_conflicting_services_are_uninstalled_before_enabling_auto_drone(installer):
    run, _, units, _, _ = installer
    unit_dirs = [units, *sorted(units.parent.glob('unit-path-*'))]
    for directory in unit_dirs:
        for unit in CONFLICTING_UNITS:
            (directory / unit).write_text('[Service]\nExecStart=/bin/true\n')
            overrides = directory / (unit + '.d')
            overrides.mkdir()
            (overrides / 'override.conf').write_text('[Service]\nRestart=always\n')
        (directory / 'unrelated.service').write_text('keep me')
    # Also exercise a masked unit (a symlink to /dev/null).
    masked = units / CONFLICTING_UNITS[0]
    masked.unlink()
    masked.symlink_to('/dev/null')
    load_states = dict.fromkeys(CONFLICTING_UNITS, 'loaded')
    load_states[CONFLICTING_UNITS[0]] = 'masked'
    result, calls = run('--start', unit_load_states=load_states)
    assert result.returncode == 0, result.stdout + result.stderr
    prepare = calls.index(['prepare-model', '--if-needed'])
    reload = calls.index(['systemctl', 'daemon-reload'])
    enable = calls.index(['systemctl', 'enable', 'Auto_Drone.service'])
    restart = calls.index(['systemctl', 'restart', 'Auto_Drone.service'])
    for unit in CONFLICTING_UNITS:
        stop = calls.index(['systemctl', 'stop', unit])
        disable = calls.index(['systemctl', 'disable', unit])
        assert prepare < stop < disable < reload < enable < restart
        for directory in unit_dirs:
            assert not (directory / unit).exists()
            assert not (directory / unit).is_symlink()
            assert not (directory / (unit + '.d')).exists()
    assert all((directory / 'unrelated.service').read_text() == 'keep me'
               for directory in unit_dirs)

    result, calls = run(unit_load_states={})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(c[:2] in (['systemctl', 'stop'], ['systemctl', 'disable'])
                   for c in calls)


@pytest.mark.parametrize('command', ['stop', 'disable'])
def test_conflicting_service_cleanup_failure_prevents_auto_drone_install(installer, command):
    run, _, units, _, _ = installer
    unit = CONFLICTING_UNITS[0]
    (units / unit).write_text('keep until stopped and disabled')
    result, calls = run('--start', unit_load_states={unit: 'loaded'},
                        service_cleanup_fails=command)
    assert result.returncode != 0
    assert (units / unit).exists()
    assert not (units / 'Auto_Drone.service').exists()
    assert not any(c[:2] in (['systemctl', 'enable'], ['systemctl', 'restart'])
                   for c in calls)
