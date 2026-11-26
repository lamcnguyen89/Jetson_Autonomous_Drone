"""Exercise real DroneKit message dispatch with in-memory MAVLink messages."""

from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()
from filtered_vehicle import FlightControllerVehicle


@pytest.fixture
def vehicle():
    handler = SimpleNamespace(
        master=SimpleNamespace(mode_mapping=lambda: {'GUIDED': 4}),
        forward_message=lambda callback: callback,
        forward_loop=lambda callback: callback,
    )
    return FlightControllerVehicle(handler)


def deliver(vehicle, message, system=42, component=1):
    encoder = mavlink.MAVLink(None, srcSystem=system, srcComponent=component)
    decoded = mavlink.MAVLink(None).parse_char(message.pack(encoder))
    vehicle.notify_message_listeners(decoded.get_type(), decoded)


def heartbeat(armed=False):
    return mavlink.MAVLink_heartbeat_message(
        mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavlink.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0,
        4, mavlink.MAV_STATE_ACTIVE, 3,
    )


@pytest.mark.parametrize('severity,level', [(7, 'DEBUG'), (6, 'INFO'), (4, 'WARNING'), (3, 'ERROR')])
def test_incoming_status_text_is_logged_once_at_original_severity(vehicle, caplog, severity, level):
    observed = []
    vehicle.add_message_listener('STATUSTEXT', lambda vehicle, name, msg: observed.append(msg.text))
    with caplog.at_level('DEBUG'):
        deliver(vehicle, mavlink.MAVLink_statustext_message(severity, b'Arm: Throttle too high'))
    records = [r for r in caplog.records if r.name == 'autopilot']
    assert len(records) == 1
    assert records[0].message == 'Arm: Throttle too high'
    assert records[0].levelname == level
    assert observed == ['Arm: Throttle too high']


@pytest.mark.parametrize('command,result,command_name,result_name,level', [
    (400, 0, 'MAV_CMD_COMPONENT_ARM_DISARM', 'MAV_RESULT_ACCEPTED', 'INFO'),
    (400, 2, 'MAV_CMD_COMPONENT_ARM_DISARM', 'MAV_RESULT_DENIED', 'WARNING'),
    (22, 5, 'MAV_CMD_NAV_TAKEOFF', 'MAV_RESULT_IN_PROGRESS', 'INFO'),
    (65000, 99, 'UNKNOWN(65000)', 'UNKNOWN(99)', 'WARNING'),
])
def test_command_ack_logs_source_result_and_preserves_listeners(
        vehicle, caplog, command, result, command_name, result_name, level):
    vehicle._trusted_fc_system_id = 42
    observed = []
    vehicle.add_message_listener('COMMAND_ACK', lambda vehicle, name, msg: observed.append(msg.command))
    with caplog.at_level('INFO'):
        deliver(vehicle, mavlink.MAVLink_command_ack_message(command, result, 50, 7))
    record, = [r for r in caplog.records if 'Flight-controller COMMAND_ACK:' in r.message]
    assert 'sysid=42 compid=1' in record.message
    assert 'command={} ({})'.format(command_name, command) in record.message
    assert 'result={} ({})'.format(result_name, result) in record.message
    assert 'progress=50 result_param2=7' in record.message
    assert record.levelname == level
    assert observed == [command]


def test_heartbeat_logging_only_records_changes_and_updates_armed_state(vehicle, caplog):
    with caplog.at_level('INFO'):
        for _ in range(50):
            deliver(vehicle, heartbeat())
        deliver(vehicle, heartbeat(armed=True))
    changes = [r.message for r in caplog.records if 'Flight-controller HEARTBEAT changed:' in r.message]
    assert len(changes) == 2
    assert 'Vehicle armed 0' in changes[0]
    assert 'Vehicle armed 1' in changes[1]
    assert vehicle.armed
    assert vehicle._trusted_fc_system_id == 42
    assert not [r for r in caplog.records if r.levelname == 'ERROR']


@pytest.mark.parametrize('name,make_message', [
    ('SYS_STATUS', lambda changed: mavlink.MAVLink_sys_status_message(
        7, 7, 3 if changed else 7, 0, 12000, 0, 90, 0, 0, 0, 0, 0, 0)),
    ('GPS_RAW_INT', lambda changed: mavlink.MAVLink_gps_raw_int_message(
        0, 3 if changed else 1, 0, 0, 0, 100, 100, 0, 0, 10)),
    ('EKF_STATUS_REPORT', lambda changed: mavlink.MAVLink_ekf_status_report_message(
        3 if changed else 1, 0, 0, 0, 0, 0)),
])
def test_health_logging_records_first_snapshot_and_changes_only(vehicle, caplog, name, make_message):
    vehicle._trusted_fc_system_id = 42
    with caplog.at_level('INFO'):
        for _ in range(50):
            deliver(vehicle, make_message(False))
        deliver(vehicle, make_message(True))
    records = [r for r in caplog.records if 'Flight-controller {} changed:'.format(name) in r.message]
    assert len(records) == 2
    assert not [r for r in caplog.records if r.levelname == 'ERROR']


@pytest.mark.parametrize('system,component', [(43, 1), (42, 191)])
def test_other_sources_are_not_reported_as_trusted_controller_events(vehicle, caplog, system, component):
    vehicle._trusted_fc_system_id = 42
    with caplog.at_level('INFO'):
        deliver(vehicle, mavlink.MAVLink_command_ack_message(400, 0), system, component)
        deliver(vehicle, heartbeat(armed=True), system, component)
    assert 'Flight-controller COMMAND_ACK:' not in caplog.text
    assert 'Flight-controller HEARTBEAT changed:' not in caplog.text
    assert not vehicle.armed
