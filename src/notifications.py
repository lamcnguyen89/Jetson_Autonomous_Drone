"""Best-effort status text and buzzer notifications over the existing MAVLink link."""

import logging
import threading
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from config import notification_settings


logger = logging.getLogger(__name__)
READY_MESSAGE = 'Human Tracking Service Ready'
ACTIVE_MESSAGE = 'Human Tracking Active'
RC9_READY_MESSAGE = 'Person Tracking Ready'
RC12_HIGH_MESSAGE = 'RC12 Wheel High'
RC12_LOW_MESSAGE = 'RC12 Wheel Low'
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
        self._rc_listening = False
        self._rc9_active = False
        self._rc12_zone = None
        self._last_rule = None
        self._deferred_rule = None
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
        logger.info('Notification event: text=%r beeps=%s text_enabled=%s beeps_enabled=%s',
                    message, beeps, self.config.text_enabled, self.config.beeps_enabled)
        sent = False
        if self.config.text_enabled and message is not None:
            logger.info('STATUSTEXT send attempt: severity=%s text=%r', severity, message)
            try:
                text = message.encode('utf-8')
                if len(text) > 50:
                    raise ValueError('Status text must fit in one 50-byte message')
                self._encoder().statustext_send(severity, text)
                logger.info('STATUSTEXT queued: src_system=%s src_component=%s severity=%s text=%r',
                            self._system_id, mavlink2.MAV_COMP_ID_ONBOARD_COMPUTER, severity, message)
                sent = True
            except Exception:
                logger.warning('Could not queue status text: %s', message, exc_info=True)
        if self.config.beeps_enabled and beeps:
            logger.info('PLAY_TUNE send attempt: beeps=%s', beeps)
            try:
                tune = beep_tune(beeps)
                self._encoder().play_tune_send(
                    self._system_id, mavlink2.MAV_COMP_ID_AUTOPILOT1, tune,
                )
                logger.debug('PLAY_TUNE queued: target_system=%s target_component=%s tune=%r',
                             self._system_id, mavlink2.MAV_COMP_ID_AUTOPILOT1, tune)
                sent = True
            except Exception:
                logger.warning('Could not queue %s buzzer beeps', beeps, exc_info=True)
        return sent

    def notify(self, message=None, beeps=0, severity=mavlink2.MAV_SEVERITY_INFO):
        """Send a rate-limited custom event. Returns whether anything was queued."""
        with self._lock:
            if self._clock() - self._last_attempt < self.config.min_interval:
                logger.debug('Notification skipped during cooldown: text=%r beeps=%s', message, beeps)
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

    def tracking_active(self):
        """Announce each autonomous takeoff, bypassing the notification cooldown."""
        with self._lock:
            return self._send(ACTIVE_MESSAGE, 2, mavlink2.MAV_SEVERITY_INFO)

    def start_rc_notifications(self):
        """Listen for RC9/RC12 test inputs, independently of flight mode/arming."""
        with self._lock:
            if self._rc_listening:
                return
            # DroneKit 2.9.2's channels attribute only consumes RC_CHANNELS_RAW;
            # read RC_CHANNELS directly so channels 9 and 12 are available.
            self.vehicle.add_message_listener('RC_CHANNELS', self._rc_channels)
            self._rc_listening = True
            logger.info('Listening for RC9 notification button: PWM 1600-2000')
            logger.info('Listening for RC12 wheel test: low PWM <=1200, high PWM >=1800; '
                        'valid PWM 800-2200, release margin 100')
            try:
                # Request all 18 RC inputs at 10 Hz on the existing connection.
                # Queuing this request does not change any RC or flight settings.
                logger.info('Requesting RC_CHANNELS telemetry: interval_us=100000 (10 Hz)')
                self._encoder().command_long_send(
                    self._system_id, mavlink2.MAV_COMP_ID_AUTOPILOT1,
                    mavlink2.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    mavlink2.MAVLINK_MSG_ID_RC_CHANNELS, 100000, 0, 0, 0, 0, 0,
                )
                logger.debug('RC_CHANNELS telemetry request queued: target_system=%s', self._system_id)
            except Exception:
                logger.warning('Could not request RC_CHANNELS; listening for existing telemetry',
                               exc_info=True)

    def _rc_channels(self, vehicle, name, message):
        # Match the flight-controller identity learned from trusted heartbeats.
        if (message.get_srcSystem() != self.vehicle._trusted_fc_system_id
                or message.get_srcComponent() != mavlink2.MAV_COMP_ID_AUTOPILOT1):
            return
        with self._lock:
            # Process each input independently: missing/unchanged RC9 must not
            # prevent RC12 from being handled in the same telemetry packet.
            if message.chancount >= 9 and message.chan9_raw not in (0, 65535):
                self._rc9_notification(message.chan9_raw)
            if message.chancount >= 12 and 800 <= message.chan12_raw <= 2200:
                self._rc12_notification(message.chan12_raw)

    def _rc9_notification(self, pwm):
        # Called under the shared lock. Missing samples never count as releases.
        active = 1600 <= pwm <= 2000
        if active == self._rc9_active:
            return
        self._rc9_active = active
        if active:
            logger.info('RC9 notification button activated: PWM=%d', pwm)
            # A deliberate button press must not be dropped by rule cooldown.
            self._send(RC9_READY_MESSAGE, 3, mavlink2.MAV_SEVERITY_INFO)
        else:
            logger.debug('RC9 notification button released: PWM=%d', pwm)

    def _rc12_notification(self, pwm):
        # Require movement away from an endpoint before it can fire again.
        # The 100us release margin prevents jitter around 1200/1800 retriggering.
        previous = self._rc12_zone
        if previous == 'high' and pwm >= 1700:
            zone = 'high'
        elif previous == 'low' and pwm <= 1300:
            zone = 'low'
        else:
            zone = 'high' if pwm >= 1800 else 'low' if pwm <= 1200 else None
        if zone == previous:
            return
        self._rc12_zone = zone
        logger.info('RC12 wheel transition: %s -> %s; PWM=%d',
                    previous or 'middle', zone or 'middle', pwm)
        if zone is not None:
            # Wheel test events bypass cooldown, just like the RC9 test button.
            message = RC12_HIGH_MESSAGE if zone == 'high' else RC12_LOW_MESSAGE
            self._send(message, 2, mavlink2.MAV_SEVERITY_INFO)

    def update_rule(self, rule_name):
        """Called each control cycle; only changed rules produce notifications.

        During cooldown, transitions are coalesced to the currently active rule.
        A short-lived intermediate rule is never replayed after it has gone away.
        Failed attempts are also deduplicated to avoid flooding logs or the link.
        """
        with self._lock:
            if rule_name == self._last_rule:
                self._deferred_rule = None
                return False
            if self._clock() - self._last_attempt < self.config.min_interval:
                if rule_name != self._deferred_rule:
                    logger.debug('Rule notification deferred during cooldown: rule=%s', rule_name)
                    self._deferred_rule = rule_name
                return False
            self._deferred_rule = None
            self._last_rule = rule_name
            if rule_name not in RULE_MESSAGES:
                logger.warning('No notification defined for rule %s', rule_name)
                return False
            count = getattr(self.config, rule_name + '_beeps')
            return self._send(RULE_MESSAGES[rule_name], count, mavlink2.MAV_SEVERITY_INFO)
