from .base import BaseRule
import enum
import config as flight_config

class Direction(enum.Enum):
    """Represent the two alternating yaw directions used during target search."""
    NONE  = -1
    LEFT  = 0
    RIGHT = 1

class SearchRule(BaseRule):
    '''
    Rule to yaw in the direction a detection was last seen in
    '''

    hasSeenPerson = False
    personDirection = Direction.NONE

    def isActive(self):
        """Activate search when no target is visible but a search history exists."""
        # A target must have been observed before search may rotate the vehicle.
        # Fresh detections activate higher-priority rules in Core instead.
        return self.hasSeenPerson

    def update(self):
        """Advance the timed search sweep using the last known target direction."""
        detection = self.camera.closestDetection()

        if detection != None:
            if detection.x < 0:
                self.personDirection = Direction.LEFT
            elif detection.x > 0:
                self.personDirection = Direction.RIGHT
            else:
                self.personDirection = Direction.NONE

            self.hasSeenPerson = True

        # With no current target, keep the last seen direction. Core separately
        # suppresses this yaw for stale perception or a temporary target lock.
        if self.personDirection is Direction.NONE:
            self._targetYaw = 0.0
        else:
            self._targetYaw = float((0.0 - flight_config.settings.yaw_rate) if self.personDirection is Direction.LEFT else flight_config.settings.yaw_rate)

    def reset(self):
        """Reset sweep timing and direction before beginning another tracking run."""
        super().reset()

        self.hasSeenPerson = False
        self.personDirection = Direction.NONE

    def name(self) -> str:
        """Return the operator-facing label for the target search behavior."""
        return 'search'
