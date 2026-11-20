"""Pilot arming gates autonomous takeoff without sending any arm commands."""

from types import SimpleNamespace

import pytest

from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

import core as module
from core import Core, ExecutionState


class PilotArmedVehicle:
    def __init__(self):
        self._armed = False
        self.mode = SimpleNamespace(name='GUIDED')
        self.is_armable = True
        self.last_heartbeat = 0
        self.location = SimpleNamespace(global_relative_frame=SimpleNamespace(alt=0))
        self.listeners = {}
        self.takeoffs = []

    def add_attribute_listener(self, name, callback):
        self.listeners[name] = callback

    @property
    def armed(self):
        return self._armed

    @armed.setter
    def armed(self, value):
        pytest.fail('Core must never send an arm command')

    def report_armed(self, armed):
        self._armed = armed
        self.listeners['armed'](self, 'armed', armed)

    def report_mode(self, mode):
        self.mode.name = mode
        self.listeners['mode'](self, 'mode', self.mode)

    def simple_takeoff(self, altitude):
        self.takeoffs.append(altitude)

    def disarm(self):
        self.report_armed(False)

    def close(self):
        pass


@pytest.fixture
def activations():
    return []


@pytest.fixture
def control(activations):
    vehicle = PilotArmedVehicle()
    camera = SimpleNamespace(available=True)
    camera.running = lambda: camera.available
    notifier = SimpleNamespace(tracking_active=lambda: activations.append('active'))
    return Core(vehicle, camera, notifier), vehicle, camera


def test_guided_and_prearm_readiness_never_arm_or_take_off(control, activations, monkeypatch):
    core, vehicle, _ = control
    vehicle.report_mode('GUIDED')
    pauses = []

    def sleep(duration):
        pauses.append(duration)
        assert core.state is ExecutionState.AwaitingArm
        assert not vehicle.armed
        assert not vehicle.takeoffs
        if len(pauses) == 3:
            core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
    assert pauses == [1, 1, 1]
    assert not activations


def test_pilot_arm_starts_delay_then_one_takeoff(control, activations, monkeypatch, caplog):
    caplog.set_level('INFO')
    core, vehicle, _ = control
    pauses = []

    def sleep(duration):
        pauses.append(duration)
        if len(pauses) == 1:
            assert core.state is ExecutionState.AwaitingArm
            vehicle.report_armed(True)
            # Flight-controller acceptance of arming is the authority here.
            vehicle.is_armable = False
        elif len(pauses) == 2:
            assert duration == 5
            assert not vehicle.takeoffs
            assert not activations
        else:
            assert core.state is ExecutionState.AwaitingReady
            assert vehicle.takeoffs == [module.settings.altitude]
            assert activations == ['active']
            core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
    assert pauses == [1, 5, 0.1]
    assert activations == ['active']
    assert 'Vehicle armed 1' in caplog.text
    assert 'Takeoff safety delay started: duration=5s' in caplog.text
    assert 'AWAITING_ARM -> TAKEOFF; reason=takeoff safety delay completed' in caplog.text
    assert 'TAKEOFF -> AWAITING_READY; reason=takeoff command queued' in caplog.text


@pytest.mark.parametrize('interruption', ['disarm', 'mode', 'connection', 'camera', 'stop'])
def test_takeoff_delay_rechecks_pilot_and_readiness(control, activations, monkeypatch, caplog, interruption):
    caplog.set_level('INFO')
    core, vehicle, camera = control
    vehicle.report_armed(True)
    pauses = []

    def sleep(duration):
        pauses.append(duration)
        if len(pauses) == 1:
            assert duration == 5
            if interruption == 'disarm':
                vehicle.report_armed(False)
            elif interruption == 'mode':
                vehicle.report_mode('LOITER')
            elif interruption == 'connection':
                vehicle.last_heartbeat = module.settings.heartbeat_timeout + 1
                core.lastHeartbeatCallback(None, None, None)
            elif interruption == 'camera':
                camera.available = False
            else:
                core.stop()
        else:
            assert not vehicle.takeoffs
            core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
    assert not vehicle.takeoffs
    assert not activations
    assert 'Takeoff safety delay cancelled:' in caplog.text


@pytest.mark.parametrize('interruption', ['disarm', 'mode', 'connection', 'camera'])
def test_takeoff_state_never_arms_or_launches_without_readiness(control, activations, monkeypatch, interruption):
    core, vehicle, camera = control
    vehicle.report_armed(True)
    if interruption == 'disarm':
        vehicle.report_armed(False)
    elif interruption == 'mode':
        vehicle.mode.name = 'LOITER'
    elif interruption == 'connection':
        vehicle.last_heartbeat = module.settings.heartbeat_timeout + 1
    else:
        camera.available = False
    core.state = ExecutionState.Takeoff

    def sleep(duration):
        assert core.state is not ExecutionState.Takeoff
        assert not vehicle.takeoffs
        core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
    assert not vehicle.takeoffs
    assert not activations


def test_activation_notification_failure_does_not_interrupt_takeoff(control, monkeypatch, caplog):
    core, vehicle, _ = control
    vehicle.report_armed(True)

    def fail():
        raise OSError('Simulated notifier failure')

    core.notifier.tracking_active = fail

    def sleep(duration):
        if duration == 0.1:
            assert core.state is ExecutionState.AwaitingReady
            assert vehicle.takeoffs == [module.settings.altitude]
            core.stop()

    monkeypatch.setattr(module.time, 'sleep', sleep)
    core.run()
    assert 'Tracking activation notification failed; takeoff continues' in caplog.text
