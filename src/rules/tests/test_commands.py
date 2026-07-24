import math
from types import SimpleNamespace

import pytest

from tests_sitl.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()

from commands import (
    altitudeErrorToVerticalVelocity,
    positionErrorToHorizontalVelocity,
    setPositionTarget,
)
from constants import ALTITUDE, MAX_VERTICAL_SPEED, SPEED
from pymavlink import mavutil


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


def test_horizontal_velocity_is_limited_by_total_speed():
    forward, right = positionErrorToHorizontalVelocity(3.0, 4.0)

    assert math.hypot(forward, right) == pytest.approx(SPEED)
    assert forward == pytest.approx(0.6)
    assert right == pytest.approx(0.8)


def test_small_horizontal_errors_are_ignored():
    assert positionErrorToHorizontalVelocity(0.1, -0.1) == (0.0, 0.0)


def test_vertical_velocity_climbs_when_below_target():
    assert altitudeErrorToVerticalVelocity(ALTITUDE - 1.0) == -MAX_VERTICAL_SPEED


def test_vertical_velocity_descends_when_above_target():
    assert altitudeErrorToVerticalVelocity(ALTITUDE + 1.0) == MAX_VERTICAL_SPEED


def test_moving_target_uses_body_velocity_and_yaw_rate():
    vehicle = RecordingVehicle(ALTITUDE)

    setPositionTarget(vehicle, (10.0, 0.0), 25.0)

    args = vehicle.message_factory.local_ned_args
    assert args is not None
    assert args[3] == mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED
    assert args[4] == 1479  # velocity + yaw rate enabled
    assert args[5:8] == (0, 0, 0)
    assert args[8] == pytest.approx(SPEED)
    assert args[9] == pytest.approx(0.0)
    assert args[10] == pytest.approx(0.0)
    assert args[14] == pytest.approx(0.0)
    assert args[15] == pytest.approx(math.radians(25.0))
    assert vehicle.sent_message == args
