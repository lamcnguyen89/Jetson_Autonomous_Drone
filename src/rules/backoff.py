from .base import BaseRule
import config as flight_config

class BackoffRule(BaseRule):
    '''
    Rule to move back if the detection is too close
    '''

    def isActive(self):
        """Activate when a current target is closer than the configured safe distance."""
        # Core evaluates backoff first, so this threshold can suppress following.
        # The camera owns target selection; Jetson's result may be a locked ID.
        return self.camera.closestDetection() != None and \
            self.camera.closestDetection().z < flight_config.settings.backoff_distance

    def update(self):
        """Refresh the proximity observation used to decide whether to back away."""
        detection = self.camera.closestDetection()
        if detection == None:
            return

        xDistance = detection.x
        zDistance = detection.z

        # A target nearer than the desired separation produces negative forward
        # error (backward motion); retain lateral centering at the same time.
        self._targetPosition = (zDistance - flight_config.settings.minimum_distance, xDistance)

    def name(self) -> str:
        """Return the operator-facing label for the proximity rule."""
        return 'backoff'
