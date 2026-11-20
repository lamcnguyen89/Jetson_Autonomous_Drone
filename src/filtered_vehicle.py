import logging

from dronekit import Vehicle
from pymavlink import mavutil


logger = logging.getLogger(__name__)
EVENT_FIELDS = {
    'HEARTBEAT': ('base_mode', 'custom_mode', 'system_status'),
    'SYS_STATUS': ('onboard_control_sensors_present', 'onboard_control_sensors_enabled',
                   'onboard_control_sensors_health'),
    'GPS_RAW_INT': ('fix_type',),
    'EKF_STATUS_REPORT': ('flags',),
}


def enum_name(enum, value):
    entry = mavutil.mavlink.enums.get(enum, {}).get(value)
    return entry.name if entry is not None else 'UNKNOWN({})'.format(value)


class FlightControllerVehicle(Vehicle):
    """
    Allows only the Pixhawk flight-controller heartbeat to update
    DroneKit's armed, mode, and connection state.
    """

    def __init__(self, handler):
        self._trusted_fc_system_id = None
        self._reported_ignored_sources = set()
        self._last_fc_events = {}
        super().__init__(handler)

    def _log_controller_message(self, name, msg):
        """Log events and telemetry changes, rather than every incoming packet."""
        source_system = msg.get_srcSystem()
        source_component = msg.get_srcComponent()
        if (source_system != self._trusted_fc_system_id
                or source_component != mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1):
            return

        if name == 'COMMAND_ACK':
            level = logging.INFO if msg.result in (
                mavutil.mavlink.MAV_RESULT_ACCEPTED, mavutil.mavlink.MAV_RESULT_IN_PROGRESS,
            ) else logging.WARNING
            logger.log(level, 'Flight-controller COMMAND_ACK: sysid=%s compid=%s '
                       'command=%s (%s) result=%s (%s) progress=%s result_param2=%s',
                       source_system, source_component, enum_name('MAV_CMD', msg.command),
                       msg.command, enum_name('MAV_RESULT', msg.result), msg.result,
                       getattr(msg, 'progress', None), getattr(msg, 'result_param2', None))
            return

        fields = EVENT_FIELDS.get(name)
        if fields is None:
            return
        snapshot = tuple(getattr(msg, field) for field in fields)
        if self._last_fc_events.get(name) == snapshot:
            return
        self._last_fc_events[name] = snapshot
        details = ' '.join('{}={}'.format(field, value) for field, value in zip(fields, snapshot))
        if name == 'HEARTBEAT':
            # Preserve the retention marker even if the service starts already armed.
            details += ' Vehicle armed {}'.format(
                int(bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)))
        elif name == 'GPS_RAW_INT':
            details += ' satellites_visible={}'.format(msg.satellites_visible)
        elif name == 'SYS_STATUS':
            details += ' voltage_battery_mv={} battery_remaining_pct={}'.format(
                msg.voltage_battery, msg.battery_remaining)
        logger.info('Flight-controller %s changed: sysid=%s compid=%s %s',
                    name, source_system, source_component, details)

    def notify_message_listeners(self, name, msg):
        if name == "HEARTBEAT":
            source_system = msg.get_srcSystem()
            source_component = msg.get_srcComponent()

            is_ardupilot_flight_controller = (
                source_component ==
                mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1
                and msg.autopilot ==
                mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
            )

            if not is_ardupilot_flight_controller:
                source = (
                    source_system,
                    source_component,
                    msg.type,
                    msg.autopilot,
                )

                # Report each unwanted source once without flooding the log.
                if source not in self._reported_ignored_sources:
                    self._reported_ignored_sources.add(source)
                    logger.warning(
                        "Ignoring non-flight-controller heartbeat: "
                        "sysid=%s compid=%s type=%s autopilot=%s "
                        "base_mode=%s custom_mode=%s",
                        source_system,
                        source_component,
                        msg.type,
                        msg.autopilot,
                        msg.base_mode,
                        msg.custom_mode,
                    )

                return

            # Lock onto the system ID of the first valid Pixhawk heartbeat.
            if self._trusted_fc_system_id is None:
                self._trusted_fc_system_id = source_system
                logger.info(
                    "Trusting flight controller heartbeat: "
                    "sysid=%s compid=%s",
                    source_system,
                    source_component,
                )

            # Ignore another autopilot using a different system ID.
            if source_system != self._trusted_fc_system_id:
                source = (source_system, source_component)
                if source not in self._reported_ignored_sources:
                    self._reported_ignored_sources.add(source)
                    logger.warning('Ignoring other flight-controller heartbeat: sysid=%s compid=%s '
                                   'trusted_sysid=%s', source_system, source_component,
                                   self._trusted_fc_system_id)
                return

        self._log_controller_message(name, msg)
        # DroneKit's autopilot logger records incoming STATUSTEXT at its MAVLink
        # severity. Keep normal dispatch so these and other listeners still run.
        # Only accepted messages reach DroneKit's normal handlers.
        super().notify_message_listeners(name, msg)
