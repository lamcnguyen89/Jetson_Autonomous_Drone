from .base import BaseRule
import config as flight_config

class BackoffRule(BaseRule):
    '''
    Rule to move back if the detection is too close
    '''

    def isActive(self):
        # Core evaluates backoff first, so this threshold can suppress following.
        # The camera owns target selection; Jetson's result may be a locked ID.
        return self.camera.closestDetection() != None and \
            self.camera.closestDetection().z < flight_config.settings.backoff_distance

    def update(self):
        detection = self.camera.closestDetection()
        if detection == None:
            return

        xDistance = detection.x
        zDistance = detection.z

        # A target nearer than the desired separation produces negative forward
        # error (backward motion); retain lateral centering at the same time.
        self._targetPosition = (zDistance - flight_config.settings.minimum_distance, xDistance)

    def name(self) -> str:
        return 'backoff'
