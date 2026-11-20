import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

import commands
from commands import (
    altitudeErrorToVerticalVelocity,
    positionErrorToHorizontalVelocity,
    setPositionTarget,
)
from config import settings
from pymavlink import mavutil


@pytest.fixture(autouse=True)
def controller_settings(monkeypatch):
    # Controller math tests must not depend on an operator's current .env tuning.
    configured = replace(
        settings, altitude=3.0, speed=1.0, follow_velocity_gain=0.4,
        follow_position_deadband=0.15, altitude_velocity_gain=1.5,
        max_vertical_speed=0.5,
    )
    monkeypatch.setattr(commands, 'settings', configured)
    return configured


class RecordingMessageFactory:
    def __init__(self):
        self.local_ned_args = None

    def set_position_target_local_ned_encode(self, *args):
        self.local_ned_args = args
        return args


class RecordingVehicle:
    def __init__(self, altitude):
        self.message_factory = RecordingMessageFactory()
        self.location = SimpleNamespace(
            global_relative_frame=SimpleNamespace(alt=altitude)
        )
        self.sent_message = None

    def send_mavlink(self, message):
        self.sent_message = message


def test_horizontal_velocity_is_limited_by_total_speed(controller_settings):
    forward, right = positionErrorToHorizontalVelocity(3.0, 4.0)

    assert math.hypot(forward, right) == pytest.approx(controller_settings.speed)
    assert forward == pytest.approx(0.6)
    assert right == pytest.approx(0.8)


def test_small_horizontal_errors_are_ignored():
    assert positionErrorToHorizontalVelocity(0.1, -0.1) == (0.0, 0.0)


def test_vertical_velocity_climbs_when_below_target(controller_settings):
    assert altitudeErrorToVerticalVelocity(controller_settings.altitude - 1.0) == -controller_settings.max_vertical_speed


def test_vertical_velocity_descends_when_above_target(controller_settings):
    assert altitudeErrorToVerticalVelocity(controller_settings.altitude + 1.0) == controller_settings.max_vertical_speed


def test_moving_target_uses_body_velocity_and_yaw_rate(controller_settings):
    vehicle = RecordingVehicle(controller_settings.altitude)

    setPositionTarget(vehicle, (10.0, 0.0), 25.0)

    args = vehicle.message_factory.local_ned_args
    assert args is not None
    assert args[3] == mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED
    assert args[4] == 1479  # velocity + yaw rate enabled
    assert args[5:8] == (0, 0, 0)
    assert args[8] == pytest.approx(controller_settings.speed)
    assert args[9] == pytest.approx(0.0)
    assert args[10] == pytest.approx(0.0)
    assert args[14] == pytest.approx(0.0)
    assert args[15] == pytest.approx(math.radians(25.0))
    assert vehicle.sent_message == args
