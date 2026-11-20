"""Flight configuration must load consistently and reject invalid tuning."""

from dataclasses import FrozenInstanceError, asdict
import math
import os
from pathlib import Path

import pytest

from config import ConfigurationError, ENV_FILE, load_settings, load_notification_settings


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / '.env'
    path.write_text((ENV_FILE.parent / '.env.example').read_text())
    return path


def set_value(path, name, value):
    lines = path.read_text().splitlines()
    path.write_text('\n'.join(
        f'{name}={value}' if line.startswith(name + '=') else line
        for line in lines
    ) + '\n')


def test_loads_numeric_values_without_changing_process_environment(env_file):
    before = dict(os.environ)
    configured = load_settings(env_file, environ={})
    assert isinstance(configured.baud_rate, int)
    assert all(isinstance(value, float) for key, value in asdict(configured).items() if key != 'baud_rate')
    assert dict(os.environ) == before
    with pytest.raises(FrozenInstanceError):
        configured.speed = 2.0


def test_default_file_is_independent_of_working_directory(tmp_path, monkeypatch):
    expected = load_settings(environ={})
    (tmp_path / '.env').write_text('ALTITUDE=999\n')
    monkeypatch.chdir(tmp_path)
    assert load_settings(environ={}) == expected
    assert ENV_FILE == Path(__file__).resolve().parents[2] / '.env'


def test_explicit_environment_overrides_file_values(env_file):
    set_value(env_file, 'ALTITUDE', '5.0')
    configured = load_settings(env_file, environ={'ALTITUDE': '7.0', 'SPEED': '0.2'})
    assert configured.altitude == 7.0
    assert configured.speed == 0.2


def test_process_environment_overrides_file_by_default(env_file, monkeypatch):
    monkeypatch.setenv('SPEED', '0.25')
    assert load_settings(env_file).speed == 0.25


def test_missing_file_has_no_fallback_to_hidden_defaults(tmp_path):
    with pytest.raises(ConfigurationError, match='copy the project .env.example'):
        load_settings(tmp_path / 'missing.env', environ={})


@pytest.mark.parametrize('name', [
    'BAUD_RATE',
    'ALTITUDE', 'ALTITUDE_FUZZINESS', 'MINIMUM_DISTANCE', 'BACKOFF_DISTANCE',
    'HEARTBEAT_TIMEOUT', 'YAW_RATE', 'YAW_MAX_DAMP', 'SPEED',
    'FOLLOW_VELOCITY_GAIN', 'FOLLOW_POSITION_DEADBAND', 'ALTITUDE_VELOCITY_GAIN',
    'MAX_VERTICAL_SPEED', 'DETECTION_THRESH',
])
def test_every_flight_setting_is_required(env_file, name):
    env_file.write_text('\n'.join(
        line for line in env_file.read_text().splitlines() if not line.startswith(name + '=')
    ))
    with pytest.raises(ConfigurationError, match=name):
        load_settings(env_file, environ={})


@pytest.mark.parametrize('value', ['', 'fast', 'nan', 'inf', '-1'])
def test_invalid_speed_is_rejected(env_file, value):
    set_value(env_file, 'SPEED', value)
    with pytest.raises(ConfigurationError, match='SPEED'):
        load_settings(env_file, environ={})


def test_baud_rate_can_be_configured_as_an_integer(env_file):
    set_value(env_file, 'BAUD_RATE', '115200')
    configured = load_settings(env_file, environ={})
    assert configured.baud_rate == 115200
    assert isinstance(configured.baud_rate, int)
    assert load_settings(env_file, environ={'BAUD_RATE': '38400'}).baud_rate == 38400


@pytest.mark.parametrize('value', ['0', '-1', '57600.5', 'nan', 'fast'])
def test_baud_rate_must_be_a_positive_integer(env_file, value):
    set_value(env_file, 'BAUD_RATE', value)
    with pytest.raises(ConfigurationError, match='BAUD_RATE'):
        load_settings(env_file, environ={})


@pytest.mark.parametrize('name,value', [
    ('ALTITUDE', '0'), ('MINIMUM_DISTANCE', '0'), ('HEARTBEAT_TIMEOUT', '0'),
    ('ALTITUDE_FUZZINESS', '3.0'), ('BACKOFF_DISTANCE', '5.0'),
    ('DETECTION_THRESH', '1.1'), ('DETECTION_THRESH', '-0.1'),
])
def test_invalid_bounds_and_relationships_are_rejected(env_file, name, value):
    set_value(env_file, name, value)
    with pytest.raises(ConfigurationError, match=name):
        load_settings(env_file, environ={})


def test_numeric_values_are_not_interpolated_or_executed(env_file, tmp_path):
    set_value(env_file, 'SPEED', '${ALTITUDE}')
    with pytest.raises(ConfigurationError, match='SPEED must be a number'):
        load_settings(env_file, environ={})
    marker = tmp_path / 'must-not-exist'
    set_value(env_file, 'SPEED', f'$(touch {marker})')
    with pytest.raises(ConfigurationError, match='SPEED must be a number'):
        load_settings(env_file, environ={})
    assert not marker.exists()


def test_env_tuning_reaches_velocity_and_following_commands(env_file, monkeypatch):
    from src.dronekit_compat import apply_dronekit_compatibility
    apply_dronekit_compatibility()
    import commands
    from rules import backoff, follow, search
    from types import SimpleNamespace

    for key, value in {
        'ALTITUDE': '5', 'SPEED': '0.2', 'MAX_VERTICAL_SPEED': '0.1',
        'MINIMUM_DISTANCE': '6', 'BACKOFF_DISTANCE': '5', 'YAW_RATE': '12',
    }.items():
        set_value(env_file, key, value)
    configured = load_settings(env_file, environ={})
    for module in (commands, backoff, follow, search):
        monkeypatch.setattr(module, 'settings', configured)

    vx, vy = commands.positionErrorToHorizontalVelocity(10.0, 10.0)
    assert math.hypot(vx, vy) == pytest.approx(0.2)
    assert commands.altitudeErrorToVerticalVelocity(4.0) == -0.1

    camera = SimpleNamespace(closestDetection=lambda: SimpleNamespace(x=2.0, z=4.0))
    follower = follow.FollowRule(None, camera)
    follower.update()
    assert follower.getState() == ((-2.0, 2.0), 12.0)
    assert backoff.BackoffRule(None, camera).isActive()
    searcher = search.SearchRule(None, camera)
    searcher.update()
    assert searcher.getState()[1] == 12.0


def test_notification_switches_and_counts_are_loaded(env_file):
    configured = load_notification_settings(env_file, environ={
        'NOTIFICATIONS_TEXT_ENABLED': 'false', 'NOTIFICATIONS_BEEPS_ENABLED': 'true',
        'NOTIFICATIONS_MIN_INTERVAL': '4.5', 'NOTIFICATIONS_FOLLOW_BEEPS': '4',
    })
    assert configured.text_enabled is False
    assert configured.beeps_enabled is True
    assert configured.min_interval == 4.5
    assert configured.follow_beeps == 4


@pytest.mark.parametrize('name,value', [
    ('NOTIFICATIONS_TEXT_ENABLED', 'maybe'), ('NOTIFICATIONS_BEEPS_ENABLED', ''),
    ('NOTIFICATIONS_MIN_INTERVAL', '-1'), ('NOTIFICATIONS_MIN_INTERVAL', 'nan'),
    ('CAMERA_READY_TIMEOUT', '0'), ('CAMERA_READY_TIMEOUT', 'inf'),
    ('NOTIFICATIONS_BACKOFF_BEEPS', '7'), ('NOTIFICATIONS_FOLLOW_BEEPS', '-1'),
    ('NOTIFICATIONS_SEARCH_BEEPS', '1.5'), ('NOTIFICATIONS_NONE_BEEPS', 'no'),
])
def test_invalid_notification_configuration_is_rejected(env_file, name, value):
    with pytest.raises(ConfigurationError, match=name):
        load_notification_settings(env_file, environ={name: value})
