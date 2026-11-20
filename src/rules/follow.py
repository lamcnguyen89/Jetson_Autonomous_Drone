from .base import BaseRule
import math
from config import settings

class FollowRule(BaseRule):
    '''
    Rule to follow a detected person
    '''

    def isActive(self):
        return self.camera.closestDetection() != None

    def update(self):
        detection = self.camera.closestDetection()
        if detection == None:
            return

        xDistance = detection.x
        zDistance = detection.z

        # Positive camera X means the person is right of center. Turn toward
        # that side with a quadratic response near center and a configured cap.
        invertYaw = xDistance < 0

        yawRate = (math.pow(xDistance, 2) / 4.0) * settings.yaw_rate * (-1 if invertYaw else 1)
        if yawRate < -settings.yaw_rate:
            yawRate = -settings.yaw_rate
        if yawRate > settings.yaw_rate:
            yawRate = settings.yaw_rate

        # Follow the desired separation, not the person's entire forward depth.
        # commands.py later converts these meter errors to bounded velocities.
        self._targetPosition = (zDistance - settings.minimum_distance, xDistance)
        self._targetYaw = yawRate

    def headingChange(self, xDistance, zDistance):
        # Geometry helper retained for callers; update() above uses the bounded
        # quadratic yaw response rather than this angle calculation.
        # Safeguard against weirdness in detection data
        if zDistance == 0: return 0

        isLeftward = xDistance < 0

        changeRadians = math.atan(abs(xDistance) / zDistance)
        changeDegrees = math.degrees(changeRadians)

        return float((0.0 - changeDegrees) if isLeftward == True else changeDegrees)

    def findHypotenuse(self, angle, zDistance):
        return math.cos(math.radians(angle)) * zDistance

    def reset(self):
        super().reset()

    def name(self) -> str:
        return 'follow'
