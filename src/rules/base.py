"""Small movement proposals evaluated in priority order by Core.

Rules observe a camera, maintain local history and return a desired command.
They do not transmit MAVLink themselves: Core selects one proposal, then the
command adapter applies velocity limits and the appropriate coordinate frame.
"""

from typing import Tuple
from camera.base import BaseCamera
from dronekit import Vehicle

class BaseRule:
    """Define the common update and setpoint interface implemented by tracking rules."""

    vehicle: Vehicle = None
    camera: BaseCamera = None

    _targetPosition = (0.0, 0.0)
    _targetYaw      = 0.0

    def __init__(self, vehicle: Vehicle, camera: BaseCamera):
        """Retain the vehicle and camera sources used by this rule."""
        self.vehicle = vehicle
        self.camera = camera

    def isActive(self) -> bool:
        """Report whether this rule should provide the current movement setpoint."""
        return False

    def update(self) -> None:
        """Refresh internal state from the latest vehicle and camera observations."""
        self._targetPosition = (0.0, 0.0)
        self._targetYaw = 0.0

    def reset(self) -> None:
        """Clear transient state before the controller starts a new flight segment."""
        self._targetPosition = (0.0, 0.0)
        self._targetYaw = 0.0

    def getState(self) -> Tuple[Tuple[float, float], float]:
        """Return forward/right position errors and a yaw rate in degrees per second."""
        # ((forward error in meters, right error in meters), yaw rate in deg/s).
        return (self._targetPosition, self._targetYaw)

    def name(self) -> str:
        """Return the rule label used in logs and operator status messages."""
        return 'base'
