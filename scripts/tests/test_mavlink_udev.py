"""MAVLink rules must grant access only to the selected serial device."""

from pathlib import Path

import pytest

from scripts.mavlink_udev import build_rule


@pytest.fixture
def usb_properties():
    return {
        "SUBSYSTEM": "tty", "ID_BUS": "usb", "ID_VENDOR_ID": "1209",
        "ID_MODEL_ID": "5741", "ID_SERIAL_SHORT": "controller-123",
        "ID_USB_INTERFACE_NUM": "00", "ID_PATH": "platform-usb-0:2.3:1.0",
    }


def test_usb_rule_tracks_identity_instead_of_tty_number(usb_properties):
    rule = build_rule(Path('/dev/ttyACM0'), usb_properties)
    assert rule == build_rule(Path('/dev/ttyACM9'), usb_properties)
    assert 'ENV{ID_SERIAL_SHORT}=="controller-123"' in rule
    assert 'ENV{ID_VENDOR_ID}=="1209"' in rule
    assert 'ENV{ID_MODEL_ID}=="5741"' in rule
    assert 'ENV{ID_USB_INTERFACE_NUM}=="00"' in rule
    assert 'KERNEL==' not in rule
    assert 'GROUP="dialout", MODE="0660"' in rule


def test_usb_without_serial_matches_physical_port(usb_properties):
    del usb_properties['ID_SERIAL_SHORT']
    rule = build_rule(Path('/dev/ttyUSB0'), usb_properties)
    assert 'ENV{ID_PATH}=="platform-usb-0:2.3:1.0"' in rule


def test_uart_rule_matches_only_the_selected_port():
    rule = build_rule(Path('/dev/ttyTHS1'), {'SUBSYSTEM': 'tty'})
    assert 'SUBSYSTEM=="tty", KERNEL=="ttyTHS1"' in rule
    assert 'GROUP="dialout", MODE="0660"' in rule
    assert 'ENV{ID_BUS}' not in rule


def test_non_serial_character_device_is_rejected():
    with pytest.raises(ValueError, match='not a tty serial device'):
        build_rule(Path('/dev/null'), {'SUBSYSTEM': 'mem'})


def test_usb_without_udev_identity_cannot_get_numbered_fallback():
    with pytest.raises(ValueError, match='USB identity is unavailable'):
        build_rule(Path('/dev/ttyACM0'), {'SUBSYSTEM': 'tty'})


def test_usb_without_serial_or_path_cannot_get_broad_rule(usb_properties):
    del usb_properties['ID_SERIAL_SHORT']
    del usb_properties['ID_PATH']
    with pytest.raises(ValueError):
        build_rule(Path('/dev/ttyACM0'), usb_properties)


@pytest.mark.parametrize('serial', ['*', 'a?b', 'a[b]', 'a|b', 'a"b', 'a\nb', '$env{X}'])
def test_unsafe_serial_falls_back_to_literal_physical_path(usb_properties, serial):
    usb_properties['ID_SERIAL_SHORT'] = serial
    rule = build_rule(Path('/dev/ttyACM0'), usb_properties)
    assert 'ENV{ID_SERIAL_SHORT}' not in rule
    assert 'ENV{ID_PATH}=="platform-usb-0:2.3:1.0"' in rule


def test_unsafe_fallback_path_is_rejected(usb_properties):
    del usb_properties['ID_SERIAL_SHORT']
    usb_properties['ID_PATH'] = '*'
    with pytest.raises(ValueError):
        build_rule(Path('/dev/ttyACM0'), usb_properties)


@pytest.mark.parametrize('key,value', [('ID_VENDOR_ID', ''), ('ID_MODEL_ID', '*'),
                                      ('ID_USB_INTERFACE_NUM', '00"')])
def test_invalid_usb_identity_fields_are_rejected(usb_properties, key, value):
    usb_properties[key] = value
    with pytest.raises(ValueError):
        build_rule(Path('/dev/ttyACM0'), usb_properties)
