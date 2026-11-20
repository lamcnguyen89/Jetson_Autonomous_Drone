"""Startup retention for flight-service logs.

Repeated connection failures can create many unarmed startup logs. Keep logs
with evidence of arming/takeoff while pruning unrelated startup attempts, once
before opening the current session's file. Retention depends on the event text
below; changes to those logged messages must preserve or update these markers.
"""

import logging
from pathlib import Path
import re


logger = logging.getLogger(__name__)

# Match events, not AWAITING_ARM, routine PreArm checks, disarming, or the
# word 'armed' in connection tracebacks. Keep legacy autonomous-arm logs too.
ARMING_EVENT = re.compile(
    r'\b(?:Waiting for arming\.\.\.|Vehicle armed (?:1|True)\b|'
    r'Arming motors\b|Arm:|takeoff mode\b|Take off to\b|'
    r'COMMAND_ACK:[^\n]*command=MAV_CMD_COMPONENT_ARM_DISARM\b)',
    re.IGNORECASE,
)


def cleanup_unarmed_logs(log_directory):
    """Delete previous .log files without an arming event; retain unreadable files."""
    removed = 0
    for path in Path(log_directory).glob('*.log'):
        # Restrict cleanup to regular local log files, leaving symlink targets
        # and unreadable/corrupt evidence untouched rather than guessing its value.
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open(encoding='utf-8') as log:
                has_arming_attempt = any(ARMING_EVENT.search(line) for line in log)
            if not has_arming_attempt:
                path.unlink()
                removed += 1
        except (OSError, UnicodeError) as error:
            logger.warning('Could not clean up log %s; retaining it: %s', path, error)
    return removed


def next_log_path(log_directory):
    """Choose a number above every retained log, even when cleanup leaves gaps."""
    directory = Path(log_directory)
    numbers = [int(path.stem) for path in directory.glob('*.log')
               if path.stem.isascii() and path.stem.isdecimal()]
    return directory / '{}.log'.format(max(numbers, default=-1) + 1)
