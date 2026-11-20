"""Flight state machine and priority-based movement selection.

Two decisions are separate: ExecutionState decides when autonomous flight may
progress, and the ordered rules decide how to move while tracking is active.
Camera backends supply measured positions through BaseCamera, while commands.py
turns the chosen position errors into MAVLink commands. No model API is needed
here, so the same controller also runs against the simulated camera.
"""

import time
import logging

from enum import Enum
from config import settings

from commands import setPositionTarget
from dronekit import Vehicle, VehicleMode
from camera.base import BaseCamera

from rules.base import BaseRule
from rules.none import NoDetectionRule
from rules.search import SearchRule
from rules.follow import FollowRule
from rules.backoff import BackoffRule

logger = logging.getLogger(__name__)

class ExecutionState(str, Enum):
    """Lifecycle states; these are independent of backoff/follow/search rule names."""
    Init           = "INIT"
    AwaitingArm    = "AWAITING_ARM"
    Takeoff        = "TAKEOFF"
    AwaitingReady  = "AWAITING_READY"
    Running        = "RUNNING"
    PilotOnly      = "PILOT_ONLY"
    ConnectionLoss = "CONNECTION_LOSS"
    Stop           = "STOP"

class Core:

    vehicle: Vehicle = None
    camera: BaseCamera = None
    state: ExecutionState = ExecutionState.Init

    activeRule = 'n/a'

    def __init__(self, vehicle: Vehicle, camera: BaseCamera, notifier=None):
        self.camera = camera
        self.vehicle = vehicle
        self.notifier = notifier
        self._notification_error_logged = False
        self.activeRule = 'n/a'

        # DroneKit delivers these callbacks from its I/O processing. They can
        # change state while the control worker is sleeping or awaiting takeoff.
        self.vehicle.add_attribute_listener('mode', self.modeCallback)
        self.vehicle.add_attribute_listener('armed', self.armedCallback)
        self.vehicle.add_attribute_listener('last_heartbeat', self.lastHeartbeatCallback)

        # Subsumption priority: the first active rule wins. Backoff overrides
        # following, a remembered target enables search, and 'none' is fallback.
        self.rules = [
            BackoffRule(self.vehicle, self.camera),
            FollowRule(self.vehicle, self.camera),
            SearchRule(self.vehicle, self.camera),
            NoDetectionRule(self.vehicle, self.camera),
        ]

        # Enter ready state
        self._set_state(ExecutionState.AwaitingArm, 'core initialized')

    def _set_state(self, state, reason):
        previous = self.state
        self.state = state
        if previous != state:
            logger.info('Core state transition: %s -> %s; reason=%s', previous.value, state.value, reason)

    #### Callbacks

    def modeCallback(self, _, _1, _2) -> None:
        logger.info('Vehicle mode %s', self.vehicle.mode)

        if self.vehicle.mode.name != "GUIDED" and self.state not in [ExecutionState.Init, ExecutionState.ConnectionLoss, ExecutionState.Stop]:
            self._set_state(ExecutionState.PilotOnly, 'vehicle left GUIDED mode')
        elif self.vehicle.mode.name == "GUIDED":
            if self.isReady() and self.isConnected():
                # An already airborne/ready vehicle must not re-enter the takeoff
                # sequence on this callback; request the existing guided hold.
                logger.warning('Cannot restart core flow due to being airborne; commanding guided hold')
                setPositionTarget(self.vehicle, (0,0), 0)
                return

            if self.vehicle.armed:
                logger.info('Requesting disarm before restarting core flow in GUIDED mode')
                self.vehicle.disarm()

            # Only reset to AwaitingArm if we're not already in a takeoff sequence
            if self.state in [ExecutionState.Init, ExecutionState.PilotOnly, ExecutionState.ConnectionLoss]:
                self._set_state(ExecutionState.AwaitingArm, 'GUIDED mode selected')

    def armedCallback(self, _, _1, _2) -> None:
        logger.info('Vehicle armed %d', self.vehicle.armed)

        if not self.vehicle.armed and self.state != ExecutionState.Init:
            self._set_state(ExecutionState.AwaitingArm, 'vehicle disarmed')

    def lastHeartbeatCallback(self, _, _1, _2) -> None:
        if not self.isConnected() and self.state != ExecutionState.Init:
            if self.state != ExecutionState.ConnectionLoss:
                logger.warning('Flight-controller heartbeat timeout: age=%.2fs limit=%.2fs',
                               self.vehicle.last_heartbeat, settings.heartbeat_timeout)
            self._set_state(ExecutionState.ConnectionLoss, 'flight-controller heartbeat timed out')

    #### Getters

    def isReady(self) -> bool:
        return self.vehicle.armed and \
                self.vehicle.mode.name == "GUIDED" and \
                self.camera.running() and \
                self.vehicle.location.global_relative_frame.alt > 0.5 # Indicates we are actually flying

    def isAltitudeOk(self) -> bool:
        # TODO: This does not account for altitude at current position, only from home
        return self.vehicle.location.global_relative_frame.alt >= settings.altitude - settings.altitude_fuzziness

    def isConnected(self) -> bool:
        return self.vehicle.last_heartbeat < settings.heartbeat_timeout

    def readyForTakeoff(self) -> bool:
        # Arming belongs to the pilot and the flight controller's normal checks.
        return self.vehicle.armed and \
                self.vehicle.mode.name == "GUIDED" and \
                self.camera.running() and \
                self.isConnected()

    #### State machine

    def _rule_command(self):
        # A stale/failed camera is different from a healthy frame with no people.
        # Do not feed stale positions to follow/backoff or trigger search yaw.
        tracking_hold = getattr(self.camera, 'tracking_hold', lambda: False)
        if not self.camera.running() or tracking_hold():
            if self.activeRule != 'perception_hold':
                logger.warning('Perception unavailable or target temporarily lost; holding tracking movement')
            self.activeRule = 'perception_hold'
            return ((0.0, 0.0), 0.0)
        previous_rule = self.activeRule
        # Update every rule's observation history, including rules suppressed by
        # a higher-priority one. Search needs the last seen direction for later.
        for rule in self.rules:
            rule.update()

        # Rule output is ((forward_error_m, right_error_m), yaw_rate_deg_per_sec).
        # commands.py owns velocity gains, limits and MAVLink coordinate frames.
        state = ((0.0, 0.0), 0.0)
        for rule in self.rules:
            if rule.isActive():
                state = rule.getState()
                self.activeRule = rule.name()
                break

        if self.activeRule != previous_rule:
            logger.info('Tracking rule transition: %s -> %s; position_error=%s yaw_rate=%s',
                        previous_rule, self.activeRule, state[0], state[1])

        if self.notifier is not None:
            try:
                # The notifier deduplicates and rate-limits repeated cycles.
                self.notifier.update_rule(self.activeRule)
                self._notification_error_logged = False
            except Exception:
                if not self._notification_error_logged:
                    logger.exception('Rule notification failed; movement control continues')
                    self._notification_error_logged = True
        return state

    def run(self) -> None:
        while not self.state is ExecutionState.Stop:

            if self.state is ExecutionState.Init:
                # Do nothing during setup.
                time.sleep(1)
                pass
            elif self.state is ExecutionState.AwaitingArm:
                if self.readyForTakeoff():
                    logger.info('Takeoff safety delay started: duration=5s altitude=%sm', settings.altitude)
                    time.sleep(5) # safety delay

                    # Pilot/connection callbacks can interrupt the delay.
                    if self.state is ExecutionState.AwaitingArm and self.readyForTakeoff():
                        self._set_state(ExecutionState.Takeoff, 'takeoff safety delay completed')
                    else:
                        logger.info('Takeoff safety delay cancelled: state=%s armed=%s mode=%s '
                                    'camera_running=%s connected=%s', self.state.value,
                                    self.vehicle.armed, self.vehicle.mode.name,
                                    self.camera.running(), self.isConnected())

                else:
                    time.sleep(1)

            elif self.state is ExecutionState.Takeoff:
                logger.info('takeoff mode')

                # Recheck immediately before takeoff; never arm or re-arm here.
                if not self.readyForTakeoff():
                    logger.warning('Takeoff cancelled by final readiness check: armed=%s mode=%s '
                                   'camera_running=%s connected=%s', self.vehicle.armed,
                                   self.vehicle.mode.name, self.camera.running(), self.isConnected())
                    if self.vehicle.mode.name != 'GUIDED':
                        self._set_state(ExecutionState.PilotOnly, 'takeoff requires GUIDED mode')
                    elif not self.isConnected():
                        self._set_state(ExecutionState.ConnectionLoss, 'takeoff requires a live connection')
                    else:
                        self._set_state(ExecutionState.AwaitingArm, 'takeoff readiness lost')
                    continue

                logger.info('Take off to %sm; queuing simple_takeoff command', settings.altitude)
                self.vehicle.simple_takeoff(settings.altitude)
                logger.info('Takeoff command queued; waiting for altitude')

                if self.notifier is not None:
                    try:
                        self.notifier.tracking_active()
                    except Exception:
                        logger.exception('Tracking activation notification failed; takeoff continues')

                # Wait until the vehicle reaches altitude
                self._set_state(ExecutionState.AwaitingReady, 'takeoff command queued')

            elif self.state is ExecutionState.AwaitingReady:
                if self.isReady() and self.isConnected() and self.isAltitudeOk():
                    # Prepare for running by resetting rules
                    for rule in self.rules:
                        rule.reset()

                    self._set_state(ExecutionState.Running, 'flight ready and target altitude reached')

                # Shouldn't really happen, but here just in case!
                elif self.vehicle.mode.name != 'GUIDED':
                    self._set_state(ExecutionState.PilotOnly, 'vehicle left GUIDED during takeoff')

                time.sleep(0.1)

            elif self.state is ExecutionState.Running:
                # Mode and armed callbacks handle exiting this state.

                # This loop can run faster than inference. Reuse a still-current
                # batch, with _rule_command guarding stale data and lost targets.
                position, yaw = self._rule_command()
                setPositionTarget(self.vehicle, position, yaw)

                time.sleep(0.01)

            elif self.state is ExecutionState.ConnectionLoss:
                # until reconnected, nothing we can do.
                time.sleep(1)
                pass

            elif self.state is ExecutionState.PilotOnly:
                # Pilot is in control. Do nothing until we have disarmed again.
                time.sleep(1)
                pass

        # Shutdown requests RTL only if the readiness, connection and altitude
        # conditions below pass; it is not an unconditional landing command.

        if self.isReady() and self.isConnected() and self.isAltitudeOk():
            # RTL delegates return/landing behavior to the flight controller.
            logger.warning('Core stopped while airborne; requesting RTL and waiting for landing')
            self.vehicle.mode = VehicleMode('RTL')
            self.vehicle.wait_for_alt(0)

        if self.vehicle.armed:
            logger.info('Requesting disarm during core shutdown')
            self.vehicle.disarm()

    def stop(self) -> None:
        self._set_state(ExecutionState.Stop, 'stop requested')

        # Cleanup
        self.vehicle.close()
