"""Live tuning is validated, shared by consumers, and deferred while armed."""

from dataclasses import replace
import json
import math
from types import SimpleNamespace

import pytest

import config
from dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()
import commands
from core import Core
from rules import backoff, follow, search


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text((config.ENV_FILE.parent / '.env.example').read_text())
    path = tmp_path / 'flight_parameters.json'
    defaults = config.load_settings(env_file, environ={}, overrides_file=path)
    monkeypatch.setattr(config, '_flight_defaults', defaults)
    monkeypatch.setattr(config, 'settings', defaults)
    monkeypatch.setattr(config, 'FLIGHT_PARAMETERS_FILE', path)
    now = [0.0]
    poller = config.FlightParameterPoller(clock=lambda: now[0])
    vehicle = SimpleNamespace(armed=False)
    return SimpleNamespace(path=path, env_file=env_file, defaults=defaults,
                           now=now, poller=poller, vehicle=vehicle)


def test_polls_every_two_seconds_without_rewriting_file(runtime, monkeypatch, caplog):
    reads = []
    read = config._read_flight_document

    def record_read(path):
        reads.append(runtime.now[0])
        return read(path)

    monkeypatch.setattr(config, '_read_flight_document', record_read)
    runtime.path.write_text('{"SPEED": 0.2}')
    original = runtime.path.read_bytes()
    with caplog.at_level('INFO'):
        for moment in (0, 1, 1.99, 2, 2, 3, 4):
            runtime.now[0] = moment
            runtime.poller.poll(runtime.vehicle)
    assert reads == [2, 4]
    assert config.settings.speed == 0.2
    assert runtime.path.read_bytes() == original
    assert caplog.text.count('Reloaded flight parameters while disarmed') == 1


@pytest.mark.parametrize('armed', [True, None])
def test_armed_or_unknown_state_skips_reads_then_applies_after_disarm(runtime, monkeypatch, armed):
    runtime.vehicle.armed = armed
    runtime.path.write_text('{"SPEED": 0.2}')
    reads = []
    load = config.load_flight_overrides

    def record_load(defaults):
        reads.append(runtime.now[0])
        return load(defaults)

    monkeypatch.setattr(config, 'load_flight_overrides', record_load)
    for moment in (2, 4, 6):
        runtime.now[0] = moment
        assert not runtime.poller.poll(runtime.vehicle)
    assert reads == []
    assert config.settings is runtime.defaults
    runtime.vehicle.armed = False
    runtime.now[0] = 8
    assert runtime.poller.poll(runtime.vehicle)
    assert reads == [8]
    assert config.settings.speed == 0.2


def test_arming_during_file_read_discards_candidate(runtime, monkeypatch):
    def load(defaults):
        runtime.vehicle.armed = True
        return replace(defaults, speed=0.2)

    monkeypatch.setattr(config, 'load_flight_overrides', load)
    runtime.now[0] = 2
    assert not runtime.poller.poll(runtime.vehicle)
    assert config.settings is runtime.defaults


@pytest.mark.parametrize('content', [
    '{', '{"SPEED": -1}', '{"SPEED": true}', '{"SPEED": NaN}',
    '{"SPEED": 0.2, "ALTITUDE": 0}', '{"BAUD_RATE": 9600}',
    '{"SPEED": 0.2, "SPEED": 0.4}',
])
def test_invalid_update_keeps_entire_previous_snapshot_and_recovers(runtime, caplog, content):
    runtime.path.write_text(content)
    runtime.now[0] = 2
    assert not runtime.poller.poll(runtime.vehicle)
    assert config.settings is runtime.defaults
    assert 'keeping last valid settings' in caplog.text
    runtime.path.write_text('{"SPEED": 0.2}')
    runtime.now[0] = 4
    assert runtime.poller.poll(runtime.vehicle)
    assert config.settings.speed == 0.2


def test_unreadable_update_keeps_snapshot_and_recovers(runtime, caplog):
    runtime.path.mkdir()
    runtime.now[0] = 2
    assert not runtime.poller.poll(runtime.vehicle)
    assert config.settings is runtime.defaults
    assert 'Cannot read' in caplog.text
    runtime.path.rmdir()
    runtime.path.write_text('{"SPEED": 0.2}')
    runtime.now[0] = 4
    assert runtime.poller.poll(runtime.vehicle)


@pytest.mark.parametrize('reset', [None, '', '{}', '{"SPEED": null}', '{"SPEED": " "}'])
def test_reset_uses_startup_defaults_without_rereading_env(runtime, monkeypatch, reset):
    def forbidden(*args):
        pytest.fail('Polling must not reread environment defaults')

    monkeypatch.setattr(config, '_read_values', forbidden)
    runtime.path.write_text('{"SPEED": 0.2}')
    runtime.now[0] = 2
    assert runtime.poller.poll(runtime.vehicle)
    runtime.env_file.write_text('SPEED=999\n')
    if reset is None:
        runtime.path.unlink()
    else:
        runtime.path.write_text(reset)
    runtime.now[0] = 4
    assert runtime.poller.poll(runtime.vehicle)
    assert config.settings == runtime.defaults


def test_reload_reaches_existing_consumers_and_ignores_display_defaults(runtime):
    camera = SimpleNamespace(closestDetection=lambda: SimpleNamespace(x=2.0, z=4.0))
    follower = follow.FollowRule(None, camera)
    backoff_rule = backoff.BackoffRule(None, camera)
    searcher = search.SearchRule(None, camera)
    vehicle = runtime.vehicle
    vehicle.add_attribute_listener = lambda *args: None
    vehicle.last_heartbeat = 2.5
    vehicle.location = SimpleNamespace(global_relative_frame=SimpleNamespace(alt=3))
    core = Core(vehicle, camera)
    values = {
        'ALTITUDE': 5, 'ALTITUDE_FUZZINESS': 0.3,
        'MINIMUM_DISTANCE': 6, 'BACKOFF_DISTANCE': 5,
        'HEARTBEAT_TIMEOUT': 3, 'YAW_RATE': 12, 'YAW_MAX_DAMP': 2,
        'SPEED': 0.2, 'FOLLOW_VELOCITY_GAIN': 0.6,
        'FOLLOW_POSITION_DEADBAND': 0.25, 'ALTITUDE_VELOCITY_GAIN': 1.2,
        'MAX_VERTICAL_SPEED': 0.1, 'DETECTION_THRESH': 0.9,
    }
    assert set(values) == config.EDITABLE_FLIGHT_KEYS
    runtime.path.write_text(json.dumps({**values, 'defaults': {'SPEED': 999}}))
    runtime.now[0] = 2
    assert runtime.poller.poll(vehicle)
    for key, value in values.items():
        assert getattr(config.settings, key.lower()) == value
    assert config.settings.baud_rate == runtime.defaults.baud_rate
    assert math.hypot(*commands.positionErrorToHorizontalVelocity(10, 10)) == pytest.approx(0.2)
    assert commands.altitudeErrorToVerticalVelocity(4) == -0.1
    follower.update()
    assert follower.getState() == ((-2.0, 2.0), 12.0)
    assert backoff_rule.isActive()
    searcher.update()
    assert searcher.getState()[1] == 12.0
    assert core.isConnected()
    assert not core.isAltitudeOk()


def test_core_loop_reloads_while_disarmed_and_freezes_while_armed(runtime, monkeypatch):
    import core as module

    vehicle = runtime.vehicle
    vehicle.mode = SimpleNamespace(name='LOITER')
    vehicle.add_attribute_listener = lambda *args: None
    vehicle.close = lambda: None
    monkeypatch.setattr(module.time, 'monotonic', lambda: runtime.now[0])
    core = Core(vehicle, SimpleNamespace(running=lambda: True))
    runtime.path.write_text('{"SPEED": 0.2}')

    def sleep(duration):
        runtime.now[0] += duration
        if runtime.now[0] == 3:
            assert config.settings.speed == 0.2
            vehicle.armed = True
            runtime.path.write_text('{"SPEED": 0.6}')
        elif runtime.now[0] == 5:
            assert config.settings.speed == 0.2
            vehicle.armed = False
        elif runtime.now[0] == 7:
            assert config.settings.speed == 0.6
            core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
