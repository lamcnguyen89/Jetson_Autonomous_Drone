from .base import BaseRule

class NoDetectionRule(BaseRule):
    '''
    Rule to just sit in the same position without yaw, waiting for initial
    detection
    '''

    def isActive(self):
        # Always eligible, but placed last in Core. BaseRule's zero position/yaw
        # output becomes a guided hold when no higher-priority rule is active.
        return True

    def name(self) -> str:
        return 'none'
