"""Best-effort status text and buzzer notifications over the existing MAVLink link."""

import logging
import threading
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from config import notification_settings


logger = logging.getLogger(__name__)
READY_MESSAGE = 'Human Tracking Service Ready'
RULE_MESSAGES = {
    'backoff': 'Tracking: Backoff - person too close',
    'follow': 'Tracking: Following person',
    'search': 'Tracking: Target lost - searching',
    'none': 'Tracking: Waiting for person',
}


def beep_tune(count):
    """A non-repeating MML tune, fitting in PLAY_TUNE's 30-byte tune field."""
    if not isinstance(count, int) or not 1 <= count <= 6:
        raise ValueError('Beep count must be an integer from 1 to 6')
    return b'MFT240L8O5' + b'CP8' * (count - 1) + b'C'


class Notifier:
    def __init__(self, vehicle, config=None, clock=time.monotonic):
        self.vehicle = vehicle
        self.config = config if config is not None else notification_settings
        self._clock = clock
        self._lock = threading.Lock()
        self._sender = None
        self._system_id = None
        self._ready_announced = False
        self._last_rule = None
        self._last_attempt = float('-inf')

    def _encoder(self):
        if self._sender is None:
            # Learned from an accepted flight-controller heartbeat by our Vehicle
            # subclass. Do not use DroneKit's default GCS source system ID (255).
            system_id = self.vehicle._trusted_fc_system_id
            if not isinstance(system_id, int) or not 1 <= system_id <= 255:
                raise RuntimeError('Flight controller identity is unavailable')
            self._sender = mavlink2.MAVLink(
                self.vehicle.message_factory.file,
                srcSystem=system_id,
                srcComponent=mavlink2.MAV_COMP_ID_ONBOARD_COMPUTER,
            )
            self._system_id = system_id
        return self._sender

    def _send(self, message, beeps, severity):
        # Called under the lock. DroneKit's MAVWriter queues bytes to its existing
        # I/O thread; no serial connection, transport write or sleep happens here.
        self._last_attempt = self._clock()
        if message is not None:
            logger.info('%s', message)
        sent = False
        if self.config.text_enabled and message is not None:
            try:
                text = message.encode('utf-8')
                if len(text) > 50:
                    raise ValueError('Status text must fit in one 50-byte message')
                self._encoder().statustext_send(severity, text)
                sent = True
            except Exception:
                logger.warning('Could not queue status text: %s', message, exc_info=True)
        if self.config.beeps_enabled and beeps:
            try:
                tune = beep_tune(beeps)
                self._encoder().play_tune_send(
                    self._system_id, mavlink2.MAV_COMP_ID_AUTOPILOT1, tune,
                )
                sent = True
            except Exception:
                logger.warning('Could not queue %s buzzer beeps', beeps, exc_info=True)
        return sent

    def notify(self, message=None, beeps=0, severity=mavlink2.MAV_SEVERITY_INFO):
        """Send a rate-limited custom event. Returns whether anything was queued."""
        with self._lock:
            if self._clock() - self._last_attempt < self.config.min_interval:
                return False
            return self._send(message, beeps, severity)

    def status_text(self, message, severity=mavlink2.MAV_SEVERITY_INFO):
        return self.notify(message, severity=severity)

    def beep(self, count):
        return self.notify(beeps=count)

    def service_ready(self):
        """Announce once per notifier/process start, independent of rule cooldown."""
        with self._lock:
            if self._ready_announced:
                return False
            self._ready_announced = True
            return self._send(READY_MESSAGE, 6, mavlink2.MAV_SEVERITY_INFO)

    def update_rule(self, rule_name):
        """Called each control cycle; only changed rules produce notifications.

        During cooldown, transitions are coalesced to the currently active rule.
        A short-lived intermediate rule is never replayed after it has gone away.
        Failed attempts are also deduplicated to avoid flooding logs or the link.
        """
        with self._lock:
            if rule_name == self._last_rule:
                return False
            if self._clock() - self._last_attempt < self.config.min_interval:
                return False
            self._last_rule = rule_name
            if rule_name not in RULE_MESSAGES:
                logger.warning('No notification defined for rule %s', rule_name)
                return False
            count = getattr(self.config, rule_name + '_beeps')
            return self._send(RULE_MESSAGES[rule_name], count, mavlink2.MAV_SEVERITY_INFO)
