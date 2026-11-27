from .base import BaseRule
import math
import config as flight_config

class FollowRule(BaseRule):
    '''
    Rule to follow a detected person
    '''

    def isActive(self):
        """Activate while a fresh target is available and no higher rule takes priority."""
        return self.camera.closestDetection() != None

    def update(self):
        """Calculate target-relative movement and yaw corrections from camera data."""
        detection = self.camera.closestDetection()
        if detection == None:
            return

        xDistance = detection.x
        zDistance = detection.z

        # Positive camera X means the person is right of center. Turn toward
        # that side with a quadratic response near center and a configured cap.
        invertYaw = xDistance < 0

        yawRate = (math.pow(xDistance, 2) / 4.0) * flight_config.settings.yaw_rate * (-1 if invertYaw else 1)
        if yawRate < -flight_config.settings.yaw_rate:
            yawRate = -flight_config.settings.yaw_rate
        if yawRate > flight_config.settings.yaw_rate:
            yawRate = flight_config.settings.yaw_rate

        # Follow the desired separation, not the person's entire forward depth.
        # commands.py later converts these meter errors to bounded velocities.
        self._targetPosition = (zDistance - flight_config.settings.minimum_distance, xDistance)
        self._targetYaw = yawRate

    def headingChange(self, xDistance, zDistance):
        """Convert the target's horizontal image offset into a bounded yaw correction."""
        # Geometry helper retained for callers; update() above uses the bounded
        # quadratic yaw response rather than this angle calculation.
        # Safeguard against weirdness in detection data
        if zDistance == 0: return 0

        isLeftward = xDistance < 0

        changeRadians = math.atan(abs(xDistance) / zDistance)
        changeDegrees = math.degrees(changeRadians)

        return float((0.0 - changeDegrees) if isLeftward == True else changeDegrees)

    def findHypotenuse(self, angle, zDistance):
        """Recover horizontal range from viewing angle and measured target depth."""
        return math.cos(math.radians(angle)) * zDistance

    def reset(self):
        """Clear prior target measurements so the next active cycle starts cleanly."""
        super().reset()

    def name(self) -> str:
        """Return the label shown while the controller follows a detected target."""
        return 'follow'
