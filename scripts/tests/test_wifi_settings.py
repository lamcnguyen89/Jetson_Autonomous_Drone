"""Exercise Wi-Fi configuration and profile fallback without touching host radios."""

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'connect_internet.sh'


def run_helper(project, commands):
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('WIFI_CONNECTIONS', 'WIFI_CONNECT_TIMEOUT')}
    return subprocess.run(
        ['bash', '-c', 'set -euo pipefail\nsource "$1"\n'
         'project_root=$2\nload_wifi_settings\n' + commands,
         'wifi-test', str(SCRIPT), str(project)],
        cwd=project.parent, env=environment, capture_output=True,
    )


def write_env(project, profiles, timeout='37'):
    text = 'WIFI_CONNECTIONS=' + shlex.quote(json.dumps(profiles)) + '\n'
    if timeout is not None:
        text += 'WIFI_CONNECT_TIMEOUT=' + shlex.quote(timeout) + '\n'
    (project / '.env').write_text(text)


def test_profile_names_remain_literal_and_ordered(tmp_path):
    project = tmp_path / 'Drone project'
    project.mkdir()
    marker = tmp_path / 'should-not-exist'
    profiles = ['Home Wi-Fi 1', "Pilot's profile", f'$(touch {marker})',
                '`false`', 'Unicode café #1', 'ends with spaces  ']
    write_env(project, profiles)
    result = run_helper(project, 'printf "%s\\0" "$CONNECT_TIMEOUT" "${WIFI_CONNECTIONS[@]}"')
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().split('\0') == ['37', *profiles, '']
    assert not marker.exists()


def test_timeout_defaults_without_virtual_environment(tmp_path):
    write_env(tmp_path, ['Saved profile'], timeout=None)
    result = run_helper(tmp_path, 'printf "%s" "$CONNECT_TIMEOUT"')
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'90'


@pytest.mark.parametrize('profiles', [[], {}, 'profile', [None], [''], ['  '],
                                     ['bad\nname'], ['bad\rname'], ['bad\0name']])
def test_invalid_profiles_stop_the_caller(tmp_path, profiles):
    write_env(tmp_path, profiles)
    result = run_helper(tmp_path, 'echo unexpected-success')
    assert result.returncode != 0
    assert b'WIFI_CONNECTIONS' in result.stderr
    assert result.stdout == b''


def test_invalid_json_is_rejected(tmp_path):
    (tmp_path / '.env').write_text("WIFI_CONNECTIONS='[broken json]'\n")
    result = run_helper(tmp_path, 'echo unexpected-success')
    assert result.returncode != 0
    assert b'WIFI_CONNECTIONS must be a JSON array' in result.stderr
    assert result.stdout == b''


@pytest.mark.parametrize('timeout', ['0', '-1', '1.5', 'abc', '01', ' 90'])
def test_invalid_timeout_is_rejected(tmp_path, timeout):
    write_env(tmp_path, ['Saved profile'], timeout)
    result = run_helper(tmp_path, 'echo unexpected-success')
    assert result.returncode != 0
    assert b'WIFI_CONNECT_TIMEOUT must be a positive integer' in result.stderr
    assert result.stdout == b''


def test_missing_env_is_reported(tmp_path):
    result = run_helper(tmp_path, 'echo unexpected-success')
    assert result.returncode != 0
    assert b'Missing' in result.stderr
    assert result.stdout == b''


def test_connection_uses_configured_timeout_and_fallback_order(tmp_path):
    write_env(tmp_path, ['Unavailable profile', 'Working profile'])
    result = run_helper(tmp_path, '''
rfkill() { printf 'rfkill %s\n' "$*"; }
nmcli() {
    case "$*" in
        'radio wifi on') return 0 ;;
        '--get-values 802-11-wireless.ssid connection show id '*) return 1 ;;
        '--get-values 802-11-wireless-security.key-mgmt connection show id '*)
            echo wpa-psk ;;
        '--ask --wait 37 connection up id Unavailable profile')
            echo 'attempt: unavailable'
            return 1 ;;
        '--ask --wait 37 connection up id Working profile')
            echo 'attempt: working'
            return 0 ;;
        *) echo "Unexpected nmcli arguments: $*" >&2; return 1 ;;
    esac
}
connect_wifi
''')
    assert result.returncode == 0, result.stderr.decode()
    assert b'rfkill unblock wifi' in result.stdout
    assert result.stdout.index(b'attempt: unavailable') < result.stdout.index(b'attempt: working')
    assert b'trying the next network' in result.stderr
