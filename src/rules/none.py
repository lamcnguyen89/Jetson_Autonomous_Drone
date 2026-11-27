from .base import BaseRule

class NoDetectionRule(BaseRule):
    '''
    Rule to just sit in the same position without yaw, waiting for initial
    detection
    '''

    def isActive(self):
        """Always activate as the final fallback when no tracking behavior applies."""
        # Always eligible, but placed last in Core. BaseRule's zero position/yaw
        # output becomes a guided hold when no higher-priority rule is active.
        return True

    def name(self) -> str:
        """Identify this fallback rule in telemetry and logs."""
        return 'none'
