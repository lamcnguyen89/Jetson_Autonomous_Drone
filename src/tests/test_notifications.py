"""Decode notification packets in memory; never connect to a flight controller."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from config import notification_settings
from notifications import Notifier, READY_MESSAGE, RULE_MESSAGES, beep_tune


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
def test_independent_channel_switches(notifier, text_enabled, beeps_enabled, expected):
    sender, writer, _ = notifier
    sender.config = replace(sender.config, text_enabled=text_enabled, beeps_enabled=beeps_enabled)
    sender.service_ready()
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


@pytest.mark.parametrize('count', [0, -1, 7, 1.5])
def test_tunes_require_short_bounded_integer_counts(count):
    with pytest.raises(ValueError):
        beep_tune(count)
