"""Flight configuration must load consistently and reject invalid tuning."""

from dataclasses import FrozenInstanceError, asdict
import json
import math
import os
from pathlib import Path

import pytest

from config import (
    ConfigurationError, EDITABLE_FLIGHT_KEYS, ENV_FILE, FLIGHT_PARAMETERS_FILE,
    load_settings, load_notification_settings, refresh_flight_parameter_defaults,
)


@pytest.fixture(autouse=True)
def overrides_file(tmp_path, monkeypatch):
    # Keep tests independent of any tuning saved by an operator in the checkout.
    path = tmp_path / 'src' / 'flight_parameters.json'
    path.parent.mkdir()
    monkeypatch.setattr('config.FLIGHT_PARAMETERS_FILE', path)
    return path


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


def test_default_file_is_independent_of_working_directory(tmp_path, monkeypatch, overrides_file):
    overrides_file.write_text('{"SPEED": 0.4}')
    expected = load_settings(environ={})
    (tmp_path / '.env').write_text('ALTITUDE=999\n')
    (tmp_path / 'flight_parameters.json').write_text('{"SPEED": 999}')
    monkeypatch.chdir(tmp_path)
    assert load_settings(environ={}) == expected
    assert expected.speed == 0.4
    assert ENV_FILE == Path(__file__).resolve().parents[2] / '.env'
    assert FLIGHT_PARAMETERS_FILE == Path(__file__).resolve().parents[1] / 'flight_parameters.json'


def test_json_overrides_reach_every_editable_flight_setting(env_file, overrides_file):
    values = {
        'ALTITUDE': 4, 'ALTITUDE_FUZZINESS': 0.3,
        'MINIMUM_DISTANCE': 5, 'BACKOFF_DISTANCE': 4,
        'HEARTBEAT_TIMEOUT': 3, 'YAW_RATE': 12, 'YAW_MAX_DAMP': 2,
        'SPEED': 0.4, 'FOLLOW_VELOCITY_GAIN': 0.6,
        'FOLLOW_POSITION_DEADBAND': 0.25, 'ALTITUDE_VELOCITY_GAIN': 1.2,
        'MAX_VERTICAL_SPEED': 0.3, 'DETECTION_THRESH': 0.9,
    }
    assert set(values) == EDITABLE_FLIGHT_KEYS
    overrides_file.write_text(json.dumps(values))
    configured = load_settings(env_file, environ={})
    assert asdict(configured) == {
        'baud_rate': 57600,
        **{key.lower(): float(value) for key, value in values.items()},
    }


def test_json_overrides_process_environment_without_writing_inputs(env_file, overrides_file, monkeypatch):
    overrides_file.write_text('{"ALTITUDE": 4, "SPEED": 0.4}')
    monkeypatch.setenv('SPEED', '0.8')
    env_before = env_file.read_bytes()
    json_before = overrides_file.read_bytes()
    process_before = dict(os.environ)
    configured = load_settings(env_file)
    assert configured.speed == 0.4
    assert configured.altitude == 4
    assert env_file.read_bytes() == env_before
    assert overrides_file.read_bytes() == json_before
    assert dict(os.environ) == process_before


@pytest.mark.parametrize('blank', [None, '', ' \t\n '])
def test_blank_json_values_use_env_defaults(env_file, overrides_file, blank):
    overrides_file.write_text(json.dumps({'SPEED': blank, 'YAW_RATE': blank}))
    assert load_settings(env_file, environ={}).speed == 1
    # Preserve the existing process-environment-over-.env precedence on reset.
    assert load_settings(env_file, environ={'SPEED': '0.8'}).speed == 0.8


@pytest.mark.parametrize('content', [None, '', ' \n', '{}'])
def test_missing_or_empty_override_file_uses_env_defaults(env_file, overrides_file, content):
    if content is not None:
        overrides_file.write_text(content)
    configured = load_settings(env_file, environ={})
    assert configured.speed == 1
    assert configured.altitude == 2


def test_explicit_override_path_can_validate_a_candidate_file(env_file, overrides_file, tmp_path):
    overrides_file.write_text('{"SPEED": 0.4}')
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{"SPEED": 0.6}')
    assert load_settings(env_file, environ={}, overrides_file=candidate).speed == 0.6
    assert load_settings(env_file, environ={}).speed == 0.4


def test_zero_override_is_not_treated_as_blank(env_file, overrides_file):
    overrides_file.write_text('{"SPEED": 0, "DETECTION_THRESH": 0}')
    configured = load_settings(env_file, environ={})
    assert configured.speed == 0
    assert configured.detection_thresh == 0


@pytest.mark.parametrize('key', [
    'BAUD_RATE', 'MAVLINK_PORT', 'SERVICE_USER', 'INFERENCE_BACKEND',
    'NOTIFICATIONS_TEXT_ENABLED', 'speed', 'UNKNOWN',
])
def test_json_cannot_override_nonflight_configuration_even_when_blank(env_file, overrides_file, key):
    overrides_file.write_text(json.dumps({key: None}))
    with pytest.raises(ConfigurationError, match=key):
        load_settings(env_file, environ={})


@pytest.mark.parametrize('content', [
    '{', '[]', 'null', 'true', '1', '"SPEED"',
    '{"SPEED": true}', '{"SPEED": false}', '{"SPEED": "0.4"}',
    '{"SPEED": []}', '{"SPEED": {}}',
    '{"SPEED": 0.4, "SPEED": null}',
    '{"SPEED": NaN}', '{"SPEED": Infinity}', '{"SPEED": 1e400}',
    '{"SPEED": -1}', '{"ALTITUDE": 0}', '{"ALTITUDE": 0.05}',
    '{"BACKOFF_DISTANCE": 5}', '{"DETECTION_THRESH": 1.1}',
])
def test_invalid_json_and_merged_flight_values_are_rejected(env_file, overrides_file, content):
    overrides_file.write_text(content)
    with pytest.raises(ConfigurationError):
        load_settings(env_file, environ={})


def test_override_read_errors_are_not_silently_ignored(env_file, overrides_file):
    overrides_file.mkdir()
    with pytest.raises(ConfigurationError, match='Cannot read'):
        load_settings(env_file, environ={})
    overrides_file.rmdir()
    overrides_file.write_bytes(b'\xff')
    with pytest.raises(ConfigurationError, match='Cannot read'):
        load_settings(env_file, environ={})


def test_generated_defaults_cannot_control_flight(env_file, overrides_file):
    overrides_file.write_text(json.dumps({
        'SPEED': '',
        'defaults': {'SPEED': 999, 'ALTITUDE': -1, 'BAUD_RATE': 0},
    }))
    configured = load_settings(env_file, environ={})
    assert configured.speed == 1
    assert configured.altitude == 2
    assert configured.baud_rate == 57600


def test_refresh_exports_fallbacks_and_preserves_overrides_and_file_access(env_file, overrides_file):
    document = {'SPEED': 0.4, 'ALTITUDE': '', 'YAW_RATE': None,
                'defaults': {'SPEED': 999, 'SERVICE_USER': 'stale'}}
    overrides_file.write_text(json.dumps(document))
    overrides_file.chmod(0o640)
    previous = overrides_file.stat()
    env_before = env_file.read_bytes()
    process_before = dict(os.environ)

    refresh_flight_parameter_defaults(env_file, environ={'SPEED': '0.8'})

    published = json.loads(overrides_file.read_text())
    assert published['SPEED'] == 0.4
    assert published['ALTITUDE'] == ''
    assert published['YAW_RATE'] is None
    assert published['defaults']['SPEED'] == 0.8
    assert published['defaults']['ALTITUDE'] == 2
    assert set(published['defaults']) == EDITABLE_FLIGHT_KEYS
    assert all(type(value) is float for value in published['defaults'].values())
    assert set(published) == EDITABLE_FLIGHT_KEYS | {'defaults'}
    assert load_settings(env_file, environ={'SPEED': '0.8'}).speed == 0.4
    assert env_file.read_bytes() == env_before
    assert dict(os.environ) == process_before
    after = overrides_file.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        previous.st_uid, previous.st_gid, previous.st_mode,
    )


def test_refresh_updates_changed_env_defaults_without_replacing_tuning(env_file, overrides_file):
    overrides_file.write_text('{"SPEED": 0.4}')
    refresh_flight_parameter_defaults(env_file, environ={})
    assert json.loads(overrides_file.read_text())['defaults']['SPEED'] == 1
    set_value(env_file, 'SPEED', '0.7')
    refresh_flight_parameter_defaults(env_file, environ={})
    published = json.loads(overrides_file.read_text())
    assert published['defaults']['SPEED'] == 0.7
    assert published['SPEED'] == 0.4


@pytest.mark.parametrize('content', [None, '', '{}'])
def test_refresh_creates_default_fields_after_reset(env_file, overrides_file, content):
    if content is not None:
        overrides_file.write_text(content)
    refresh_flight_parameter_defaults(env_file, environ={})
    published = json.loads(overrides_file.read_text())
    assert set(published['defaults']) == EDITABLE_FLIGHT_KEYS
    assert all(published[key] is None for key in EDITABLE_FLIGHT_KEYS)
    assert load_settings(env_file, environ={}).speed == published['defaults']['SPEED']


def test_refresh_write_failure_keeps_original_document(env_file, overrides_file, monkeypatch):
    overrides_file.write_text('{"SPEED": 0.4}')
    before = overrides_file.read_bytes()

    def fail_replace(*args):
        raise PermissionError('Read-only destination')

    monkeypatch.setattr('config.os.replace', fail_replace)
    with pytest.raises(ConfigurationError, match='Cannot publish flight defaults'):
        refresh_flight_parameter_defaults(env_file, environ={})
    assert overrides_file.read_bytes() == before
    assert list(overrides_file.parent.iterdir()) == [overrides_file]


def test_refresh_rejects_invalid_overrides_without_rewriting_them(env_file, overrides_file):
    overrides_file.write_text('{"SPEED": -1}')
    before = overrides_file.read_bytes()
    with pytest.raises(ConfigurationError, match='SPEED'):
        refresh_flight_parameter_defaults(env_file, environ={})
    assert overrides_file.read_bytes() == before


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


def test_json_tuning_reaches_velocity_commands(env_file, overrides_file, monkeypatch):
    from src.dronekit_compat import apply_dronekit_compatibility
    apply_dronekit_compatibility()
    import commands

    overrides_file.write_text('{"ALTITUDE": 5, "SPEED": 0.2, "MAX_VERTICAL_SPEED": 0.1}')
    monkeypatch.setattr(commands, 'settings', load_settings(env_file, environ={}))
    vx, vy = commands.positionErrorToHorizontalVelocity(10.0, 10.0)
    assert math.hypot(vx, vy) == pytest.approx(0.2)
    assert commands.altitudeErrorToVerticalVelocity(4.0) == -0.1


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
