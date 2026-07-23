import logging

from dronekit import Vehicle
from pymavlink import mavutil


logger = logging.getLogger(__name__)


class FlightControllerVehicle(Vehicle):
    """
    Allows only the Pixhawk flight-controller heartbeat to update
    DroneKit's armed, mode, and connection state.
    """

    def __init__(self, handler):
        self._trusted_fc_system_id = None
        self._reported_ignored_sources = set()
        super().__init__(handler)

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
                return

        # Only accepted messages reach DroneKit's normal handlers.
        super().notify_message_listeners(name, msg)