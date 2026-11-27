"""Perception interface consumed by Core and the movement rules.

Rules ask for positions and health without knowing where inference runs. The
real backends extend PerceptionCamera; simulation supplies its own MockCamera.
This base class provides defaults, not hardware acquisition or freshness checks.
"""

class BaseCamera:
    """Define the camera lifecycle and observation API consumed by flight rules."""

    def __init__(self):
        """Initialize the shared camera base state."""
        pass

    def running(self):
        """Whether the backend is usable; real backends also enforce freshness."""
        return False

    def tracking_hold(self):
        """True while a tracker reserves a missing target instead of selecting another."""
        return False

    def detections(self):
        """Return the current target observation, if the backend has one."""
        return []

    def start(self):
        """Start image capture; subclasses implement backend-specific startup."""
        pass

    def stop(self):
        """Release capture resources; subclasses implement backend-specific shutdown."""
        pass

    def closestDetection(self):
        """Default selection is nearest depth; Jetson overrides this with its ID lock."""
        closest = None

        for detection in self.detections():
            if closest == None: closest = detection
            elif closest.z > detection.z: closest = detection

        return closest
