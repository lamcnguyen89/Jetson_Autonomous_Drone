"""Decode notification packets in memory; never connect to a flight controller."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from config import notification_settings
from notifications import (Notifier, READY_MESSAGE, ACTIVE_MESSAGE, RC9_READY_MESSAGE,
                           RC12_HIGH_MESSAGE, RC12_LOW_MESSAGE, RULE_MESSAGES, beep_tune)


class Writer:
    def __init__(self, fail_types=()):
        self.packets = []
        self.attempts = 0
        self.fail_types = fail_types

    def write(self, packet):
        self.attempts += 1
        message = mavlink.MAVLink(None).parse_char(packet)
        if message.get_type() in self.fail_types:
            raise OSError('Simulated queue failure')
        self.packets.append(bytes(packet))

    def messages(self):
        return [mavlink.MAVLink(None).parse_char(packet) for packet in self.packets]


@pytest.fixture
def notifier():
    writer = Writer()
    vehicle = SimpleNamespace(_trusted_fc_system_id=42, message_factory=SimpleNamespace(file=writer))
    clock = [0.0]
    config = replace(notification_settings, text_enabled=True, beeps_enabled=True,
                     min_interval=3.0, backoff_beeps=3, follow_beeps=1, search_beeps=2, none_beeps=0)
    return Notifier(vehicle, config, lambda: clock[0]), writer, clock


@pytest.fixture
def rc9_button(notifier):
    sender, writer, clock = notifier
    listeners = {}
    sender.vehicle.add_message_listener = lambda name, callback: listeners.setdefault(name, callback)
    sender.start_rc_notifications()

    def emit(pwm, count=18, system=42, component=mavlink.MAV_COMP_ID_AUTOPILOT1, rc12=65535):
        channels = [1500] * 8 + [pwm] + [65535] * 9
        channels[11] = rc12
        message = mavlink.MAVLink_rc_channels_message(0, count, *channels, 255)
        encoder = mavlink.MAVLink(None, srcSystem=system, srcComponent=component)
        decoded = mavlink.MAVLink(None).parse_char(message.pack(encoder))
        listeners['RC_CHANNELS'](sender.vehicle, 'RC_CHANNELS', decoded)

    return sender, writer, clock, emit


@pytest.fixture
def rc12_wheel(rc9_button):
    sender, writer, clock, emit_channels = rc9_button

    def emit(pwm, count=18, system=42, component=1, rc9=1500):
        emit_channels(rc9, count=count, system=system, component=component, rc12=pwm)

    return sender, writer, clock, emit


@pytest.mark.parametrize('pwm,text', [
    (800, 'RC12 Wheel Low'), (1000, 'RC12 Wheel Low'), (1200, 'RC12 Wheel Low'),
    (1201, None), (1500, None), (1799, None),
    (1800, 'RC12 Wheel High'), (2000, 'RC12 Wheel High'), (2200, 'RC12 Wheel High'),
    (799, None), (2201, None), (0, None), (65535, None),
])
def test_rc12_endpoints_send_distinct_text_and_two_beeps(rc12_wheel, pwm, text):
    _, writer, _, emit = rc12_wheel
    emit(pwm)
    messages = writer.messages()[1:]
    if text is None:
        assert not messages
        return
    status, tune = messages
    assert status.get_type() == 'STATUSTEXT' and status.text == text
    assert status.severity == mavlink.MAV_SEVERITY_INFO
    assert tune.get_type() == 'PLAY_TUNE' and tune.tune == 'MFT240L8O5CP8C'
    assert tune.target_system == 42 and tune.target_component == 1
    assert status.get_srcSystem() == 42 and status.get_srcComponent() == 191


def test_rc12_holding_jitter_and_missing_samples_do_not_retrigger(rc12_wheel, caplog):
    _, writer, _, emit = rc12_wheel
    with caplog.at_level('INFO'):
        for pwm in [1500, 1800, 2000, 1799, 1700, 0, 65535, 2201]:
            emit(pwm)
        emit(1000, count=11)  # No valid RC12 sample, so keep the previous endpoint.
        emit(1800)
        assert len(writer.messages()) == 3  # One stream request and one notification pair.
        emit(1699)  # Leave the high endpoint's hysteresis band.
        emit(1800)
        emit(1000)  # Direct high-to-low movement also triggers.
        for pwm in [1201, 1300, 1200]:
            emit(pwm)
        emit(1301)  # Leave the low endpoint's hysteresis band.
        emit(1200)
    statuses = [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT']
    assert statuses == [RC12_HIGH_MESSAGE, RC12_HIGH_MESSAGE, RC12_LOW_MESSAGE, RC12_LOW_MESSAGE]
    assert caplog.text.count('RC12 wheel transition: middle -> high; PWM=1800') == 2
    assert 'RC12 wheel transition: high -> low; PWM=1000' in caplog.text


def test_rc12_bypasses_startup_and_rule_cooldowns(rc12_wheel):
    sender, writer, _, emit = rc12_wheel
    sender.service_ready()
    emit(2000)
    emit(1000)
    assert [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT'] == [
        READY_MESSAGE, RC12_HIGH_MESSAGE, RC12_LOW_MESSAGE,
    ]
    assert not sender.update_rule('follow')


@pytest.mark.parametrize('system,component', [(43, 1), (42, 191), (255, 190)])
def test_rc12_ignores_foreign_activations_and_releases(rc12_wheel, system, component):
    _, writer, _, emit = rc12_wheel
    emit(1000, system=system, component=component)
    assert len(writer.messages()) == 1
    emit(2000)
    emit(1500, system=system, component=component)
    emit(2000)
    assert len(writer.messages()) == 3


@pytest.mark.parametrize('rc9', [0, 65535, 1800])
def test_rc12_is_processed_independently_of_rc9(rc12_wheel, rc9):
    _, writer, _, emit = rc12_wheel
    emit(2000, rc9=rc9)
    emit(1000, rc9=rc9)
    statuses = [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT']
    expected = [RC9_READY_MESSAGE] if rc9 == 1800 else []
    assert statuses == expected + [RC12_HIGH_MESSAGE, RC12_LOW_MESSAGE]


@pytest.mark.parametrize('text_enabled,beeps_enabled,expected', [
    (True, False, ['STATUSTEXT']), (False, True, ['PLAY_TUNE']), (False, False, []),
])
def test_rc12_honors_notification_switches(rc12_wheel, text_enabled, beeps_enabled, expected):
    sender, writer, _, emit = rc12_wheel
    sender.config = replace(sender.config, text_enabled=text_enabled, beeps_enabled=beeps_enabled)
    emit(2000)
    assert [m.get_type() for m in writer.messages()[1:]] == expected


def test_rc12_failed_sends_do_not_retry_until_wheel_moves_away(rc12_wheel):
    _, writer, _, emit = rc12_wheel
    writer.fail_types = ('STATUSTEXT', 'PLAY_TUNE')
    emit(2000)
    attempts = writer.attempts
    for _ in range(20):
        emit(2000)
    assert writer.attempts == attempts
    emit(1500)
    emit(2000)
    assert writer.attempts == attempts + 2


def test_rc12_still_works_when_telemetry_request_fails(notifier):
    sender, writer, _ = notifier
    writer.fail_types = ('COMMAND_LONG',)
    listeners = {}
    sender.vehicle.add_message_listener = lambda name, callback: listeners.setdefault(name, callback)
    sender.start_rc_notifications()
    message = SimpleNamespace(chancount=12, chan9_raw=65535, chan12_raw=2000,
                              get_srcSystem=lambda: 42, get_srcComponent=lambda: 1)
    listeners['RC_CHANNELS'](sender.vehicle, 'RC_CHANNELS', message)
    assert [m.get_type() for m in writer.messages()] == ['STATUSTEXT', 'PLAY_TUNE']


def test_rc9_requests_full_channel_telemetry_once(rc9_button):
    sender, writer, _, _ = rc9_button
    sender.start_rc_notifications()
    request, = writer.messages()
    assert request.get_type() == 'COMMAND_LONG'
    assert request.command == mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
    assert request.target_system == 42
    assert request.target_component == mavlink.MAV_COMP_ID_AUTOPILOT1
    assert request.param1 == mavlink.MAVLINK_MSG_ID_RC_CHANNELS
    assert request.param2 == 100000  # 10 Hz, expressed in microseconds.


@pytest.mark.parametrize('pwm,active', [
    (1599, False), (1600, True), (1800, True), (2000, True), (2001, False),
    (0, False), (65535, False),
])
def test_rc9_pwm_range_and_exact_notification(rc9_button, pwm, active):
    _, writer, _, emit = rc9_button
    emit(pwm)
    messages = writer.messages()[1:]  # Skip the stream request.
    if not active:
        assert not messages
        return
    text, tune = messages
    assert text.get_type() == 'STATUSTEXT' and text.text == 'Person Tracking Ready'
    assert text.severity == mavlink.MAV_SEVERITY_INFO
    assert tune.get_type() == 'PLAY_TUNE' and tune.tune == 'MFT240L8O5CP8CP8C'
    assert tune.target_system == 42 and tune.target_component == mavlink.MAV_COMP_ID_AUTOPILOT1


def test_rc9_announces_once_per_press_even_during_cooldown(rc9_button, caplog):
    sender, writer, _, emit = rc9_button
    sender.service_ready()
    emit(1000)
    with caplog.at_level('INFO'):
        emit(1800)
    for pwm in [1600, 1700, 2000, 0, 65535, 1800]:
        emit(pwm)
    emit(1000, count=8)  # No RC9 value available; this is not a release.
    emit(1800)
    texts = [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT']
    assert texts == [READY_MESSAGE, RC9_READY_MESSAGE]
    emit(1500)
    emit(1600)
    texts = [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT']
    assert texts == [READY_MESSAGE, RC9_READY_MESSAGE, RC9_READY_MESSAGE]
    assert not sender.update_rule('follow')
    assert 'RC9 notification button activated: PWM=1800' in caplog.text


@pytest.mark.parametrize('system,component', [(43, 1), (42, 191), (255, 190)])
def test_rc9_ignores_other_mavlink_sources(rc9_button, system, component):
    _, writer, _, emit = rc9_button
    emit(1800, system=system, component=component)
    assert len(writer.messages()) == 1  # Only our stream request.
    emit(1800)
    emit(1000, system=system, component=component)
    emit(1800)
    assert len(writer.messages()) == 3  # A foreign release cannot retrigger it.


def test_rc9_stream_request_failure_keeps_listener_working(notifier):
    sender, writer, _ = notifier
    writer.fail_types = ('COMMAND_LONG',)
    listeners = {}
    sender.vehicle.add_message_listener = lambda name, callback: listeners.setdefault(name, callback)
    sender.start_rc_notifications()
    # Receiving telemetry that was already enabled still works after request failure.
    message = SimpleNamespace(chancount=18, chan9_raw=1800, chan12_raw=65535,
                              get_srcSystem=lambda: 42, get_srcComponent=lambda: 1)
    listeners['RC_CHANNELS'](sender.vehicle, 'RC_CHANNELS', message)
    assert [m.get_type() for m in writer.messages()] == ['STATUSTEXT', 'PLAY_TUNE']


def test_rc9_failed_sends_do_not_repeat_while_held(rc9_button):
    _, writer, _, emit = rc9_button
    writer.fail_types = ('STATUSTEXT', 'PLAY_TUNE')
    emit(1800)
    attempts = writer.attempts
    for _ in range(10):
        emit(1800)
    assert writer.attempts == attempts
    emit(1000)
    emit(1800)
    assert writer.attempts == attempts + 2


@pytest.mark.parametrize('text_enabled,beeps_enabled,expected', [
    (True, False, ['STATUSTEXT']), (False, True, ['PLAY_TUNE']), (False, False, []),
])
def test_rc9_honors_notification_switches(rc9_button, text_enabled, beeps_enabled, expected):
    sender, writer, _, emit = rc9_button
    sender.config = replace(sender.config, text_enabled=text_enabled, beeps_enabled=beeps_enabled)
    emit(1800)
    assert [m.get_type() for m in writer.messages()[1:]] == expected


def test_ready_is_once_and_has_six_beeps_with_correct_wire_identity(notifier):
    sender, writer, _ = notifier
    assert sender.service_ready()
    assert not sender.service_ready()
    text, tune = writer.messages()
    assert text.get_type() == 'STATUSTEXT' and text.text == READY_MESSAGE
    assert text.severity == mavlink.MAV_SEVERITY_INFO
    assert tune.get_type() == 'PLAY_TUNE' and tune.tune.count('C') == 6
    assert tune.tune == 'MFT240L8O5CP8CP8CP8CP8CP8C'
    assert tune.target_system == 42 and tune.target_component == mavlink.MAV_COMP_ID_AUTOPILOT1
    for packet, message in zip(writer.packets, writer.messages()):
        assert packet[0] == 0xFD  # MAVLink 2, even if DroneKit's factory is v1.
        assert message.get_srcSystem() == 42
        assert message.get_srcComponent() == mavlink.MAV_COMP_ID_ONBOARD_COMPUTER


def test_activation_has_exact_text_and_two_beeps_even_during_cooldown(notifier):
    sender, writer, clock = notifier
    sender.service_ready()
    clock[0] = 0.5
    assert sender.tracking_active()
    text, tune = writer.messages()[-2:]
    assert text.get_type() == 'STATUSTEXT' and text.text == 'Human Tracking Active'
    assert text.severity == mavlink.MAV_SEVERITY_INFO
    assert tune.get_type() == 'PLAY_TUNE' and tune.tune == 'MFT240L8O5CP8C'
    assert tune.target_system == 42 and tune.target_component == mavlink.MAV_COMP_ID_AUTOPILOT1
    assert not sender.update_rule('follow')
    clock[0] = 3.5
    assert sender.update_rule('follow')
    # A later flight must get its own activation announcement.
    assert sender.tracking_active()
    assert [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT'].count(ACTIVE_MESSAGE) == 2


@pytest.mark.parametrize('rule,count', [('backoff', 3), ('follow', 1), ('search', 2), ('none', 0)])
def test_rule_messages_and_patterns(notifier, rule, count):
    sender, writer, _ = notifier
    assert sender.update_rule(rule)
    messages = writer.messages()
    assert messages[0].text == RULE_MESSAGES[rule]
    assert len(messages) == (2 if count else 1)
    if count:
        assert messages[1].tune.count('C') == count


def test_repeated_cycles_and_cooldown_only_announce_current_rule(notifier):
    sender, writer, clock = notifier
    sender.update_rule('follow')
    for _ in range(100):
        assert not sender.update_rule('follow')
    clock[0] = 1.0
    assert not sender.update_rule('search')
    clock[0] = 2.0
    assert not sender.update_rule('backoff')
    clock[0] = 3.0
    assert sender.update_rule('backoff')  # No fresh transition is needed after cooldown.
    clock[0] = 10.0
    assert not sender.update_rule('backoff')
    assert [m.text for m in writer.messages() if m.get_type() == 'STATUSTEXT'] == [
        RULE_MESSAGES['follow'], RULE_MESSAGES['backoff'],
    ]


def test_brief_intermediate_rule_is_not_replayed(notifier):
    sender, writer, clock = notifier
    sender.update_rule('follow')
    clock[0] = 1.0
    sender.update_rule('search')
    clock[0] = 4.0
    sender.update_rule('follow')
    assert len(writer.messages()) == 2


def test_ready_and_rules_share_cooldown(notifier):
    sender, writer, clock = notifier
    sender.service_ready()
    assert not sender.update_rule('none')
    clock[0] = 3.0
    assert sender.update_rule('none')
    assert len(writer.messages()) == 3


@pytest.mark.parametrize('text_enabled,beeps_enabled,expected', [
    (True, False, ['STATUSTEXT']), (False, True, ['PLAY_TUNE']), (False, False, []),
])
@pytest.mark.parametrize('event', ['service_ready', 'tracking_active'])
def test_independent_channel_switches(notifier, text_enabled, beeps_enabled, expected, event):
    sender, writer, _ = notifier
    sender.config = replace(sender.config, text_enabled=text_enabled, beeps_enabled=beeps_enabled)
    getattr(sender, event)()
    assert [m.get_type() for m in writer.messages()] == expected
    if writer.messages():
        assert writer.messages()[0].get_srcSystem() == 42


def test_text_failure_does_not_prevent_buzzer(notifier, caplog):
    sender, writer, _ = notifier
    writer.fail_types = ('STATUSTEXT',)
    assert sender.service_ready()
    assert [m.get_type() for m in writer.messages()] == ['PLAY_TUNE']
    assert 'Could not queue status text' in caplog.text


def test_send_failures_are_nonfatal_and_not_retried_every_cycle(notifier, caplog):
    sender, writer, _ = notifier
    writer.fail_types = ('STATUSTEXT', 'PLAY_TUNE')
    assert not sender.update_rule('follow')
    for _ in range(100):
        assert not sender.update_rule('follow')
    assert writer.attempts == 2
    assert 'Could not queue' in caplog.text


def test_unknown_controller_identity_does_not_broadcast_to_arbitrary_system(notifier):
    sender, writer, _ = notifier
    sender.vehicle._trusted_fc_system_id = None
    assert not sender.service_ready()
    assert not writer.packets


def test_custom_notifications_and_channel_helpers(notifier):
    sender, writer, clock = notifier
    assert sender.status_text('Custom event', mavlink.MAV_SEVERITY_NOTICE)
    assert not sender.beep(2)
    clock[0] = 3.0
    assert sender.beep(2)
    text, tune = writer.messages()
    assert text.text == 'Custom event' and text.severity == mavlink.MAV_SEVERITY_NOTICE
    assert tune.tune.count('C') == 2


@pytest.mark.parametrize('fail', [False, True])
def test_status_text_logs_attempt_before_queue_and_reports_outcome(notifier, caplog, monkeypatch, fail):
    sender, writer, _ = notifier
    original_write = writer.write

    def write(packet):
        # The attempt must already be recorded, even if the queue write fails.
        assert "STATUSTEXT send attempt: severity=4 text='Debug test'" in caplog.text
        if fail:
            raise OSError('Queue unavailable')
        return original_write(packet)

    monkeypatch.setattr(writer, 'write', write)
    with caplog.at_level('INFO'):
        assert sender.status_text('Debug test', mavlink.MAV_SEVERITY_WARNING) is not fail
    if fail:
        assert 'Could not queue status text: Debug test' in caplog.text
        assert 'Queue unavailable' in caplog.text
        assert 'STATUSTEXT queued:' not in caplog.text
    else:
        assert 'STATUSTEXT queued: src_system=42 src_component=191 severity=4' in caplog.text
        assert writer.messages()[0].text == 'Debug test'


def test_disabled_text_is_distinguishable_from_a_send_attempt(notifier, caplog):
    sender, writer, _ = notifier
    sender.config = replace(sender.config, text_enabled=False)
    with caplog.at_level('INFO'):
        assert not sender.status_text('Disabled test')
    assert "text='Disabled test' beeps=0 text_enabled=False" in caplog.text
    assert 'STATUSTEXT send attempt' not in caplog.text
    assert not writer.packets


def test_rule_cooldown_is_logged_once_per_pending_rule(notifier, caplog):
    sender, writer, clock = notifier
    sender.service_ready()
    with caplog.at_level('DEBUG'):
        for _ in range(100):
            sender.update_rule('follow')
        sender.update_rule('search')
        clock[0] = 3
        sender.update_rule('search')
    assert caplog.text.count('Rule notification deferred during cooldown: rule=follow') == 1
    assert caplog.text.count('Rule notification deferred during cooldown: rule=search') == 1
    assert writer.messages()[-2].text == RULE_MESSAGES['search']


@pytest.mark.parametrize('count', [0, -1, 7, 1.5])
def test_tunes_require_short_bounded_integer_counts(count):
    with pytest.raises(ValueError):
        beep_tune(count)
