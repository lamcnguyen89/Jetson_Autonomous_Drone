"""Translate rule outputs into flight-controller setpoints.

Rules express forward/right position errors in meters and yaw rates in degrees
per second. Moving commands use bounded proportional velocities in the vehicle
body frame; the zero-error branch holds a captured geographic position instead.
Altitude is relative to home and is controlled separately from camera-space Y.
"""

from typing import Tuple
from pymavlink import mavutil
from dronekit import Vehicle
import config as flight_config

import math

IS_LOITER = False
# Cache the geographic point when entering hold. Reusing it prevents repeated
# commands from moving the hold point along with any aircraft drift.
LOITER_POSITION = {
    "latitude": 0,
    "longitude": 0
}

# Restrict a controller output to a safe minimum and maximum value.
def clamp(value: float, minimum: float, maximum: float) -> float:
    """Restrict a controller output to an inclusive lower and upper bound."""
    return max(minimum, min(value, maximum))

# Convert forward/right position errors in meters into body-frame velocities.
# Small errors are ignored to prevent jitter, and the combined horizontal
# velocity is limited so diagonal movement cannot exceed the configured speed.
def positionErrorToHorizontalVelocity(
    forwardError: float,
    rightError: float,
) -> Tuple[float, float]:
    """Apply deadband and proportional gain, then cap combined horizontal speed."""
    if abs(forwardError) <= flight_config.settings.follow_position_deadband:
        forwardError = 0.0
    if abs(rightError) <= flight_config.settings.follow_position_deadband:
        rightError = 0.0

    forwardVelocity = forwardError * flight_config.settings.follow_velocity_gain
    rightVelocity = rightError * flight_config.settings.follow_velocity_gain

    # Limit total horizontal speed, including diagonal movement.
    magnitude = math.hypot(forwardVelocity, rightVelocity)
    if magnitude > flight_config.settings.speed:
        scale = flight_config.settings.speed / magnitude
        forwardVelocity *= scale
        rightVelocity *= scale

    return forwardVelocity, rightVelocity

# Convert home-relative altitude error into an NED vertical velocity.
# NED is positive downward, so being below the target altitude produces a negative
# velocity (climb); the result is capped for a controlled correction.
def altitudeErrorToVerticalVelocity(currentAltitude: float) -> float:
    """Translate altitude error into a speed-limited NED vertical velocity."""
    # MAVLink NED uses positive Z velocity for down and negative for up.
    verticalVelocity = (
        currentAltitude - flight_config.settings.altitude
    ) * flight_config.settings.altitude_velocity_gain
    return clamp(
        verticalVelocity,
        -flight_config.settings.max_vertical_speed,
        flight_config.settings.max_vertical_speed,
    )

def setYaw(vehicle: Vehicle, relativeYaw: float) -> None:
    """Queue a relative yaw command using MAVLink's condition-yaw command."""
    msg = vehicle.message_factory.command_long_encode(
        0, 0,    # target system, target component
        mavutil.mavlink.MAV_CMD_CONDITION_YAW,
        0, #confirmation
        abs(relativeYaw),    # param 1, yaw in degrees
        0,          # param 2, yaw speed deg/s
        1 if relativeYaw >= 0 else -1, # param 3, direction -1 ccw, 1 cw
        1, # param 4, relative offset 1, absolute angle 0
        0, 0, 0)    # param 5 ~ 7 not used

    vehicle.send_mavlink(msg)
    vehicle.flush()


"""

This target position function was created in order to deal with the issue that when the drone moves horizontally, it begins to drift towards the ground. This function calculates the velocity in the NED frame based on the position offsets and sends it to the drone, which should help maintain altitude while moving horizontally.

"""

def setPositionTarget(vehicle: Vehicle, position: Tuple[float, float], yawRate: float) -> None:
    """Send body-frame velocity when moving, or geographic hold at zero XY error."""
    global IS_LOITER

    forwardError, rightError = position

    if forwardError == 0 and rightError == 0:
        # Loiter in place with guided mode
        setLoiterGuided(vehicle, yawRate)
    else:
        IS_LOITER = False
        # Moving again ends the current hold episode; the next hold captures a
        # new point. This module assumes a single active vehicle/controller.

        forwardVelocity, rightVelocity = positionErrorToHorizontalVelocity(
            forwardError,
            rightError,
        )
        currentAltitude = vehicle.location.global_relative_frame.alt
        verticalVelocity = altitudeErrorToVerticalVelocity(currentAltitude)

        # Enable velocity and yaw-rate; ignore position, acceleration, and yaw.
        ignorePositionMask = 0b111
        ignoreAccelMask = 0b111000000
        ignoreYaw = 0b10000000000
        type_mask = ignorePositionMask | ignoreAccelMask | ignoreYaw

        msg = vehicle.message_factory.set_position_target_local_ned_encode(
            0,       # time_boot_ms (not used)
            0, 0,    # target system, target component
            mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED,
            type_mask,
            0, 0, 0, # x, y, z position (not used)
            forwardVelocity, rightVelocity, verticalVelocity,
            0, 0, 0, # x, y, z acceleration (not used)
            0, math.radians(yawRate))    # yaw, yaw_rate

        vehicle.send_mavlink(msg)



def setLoiterGuided(vehicle: Vehicle, yawRate: float) -> None:
    """Hold the saved latitude/longitude at configured altitude, allowing search yaw."""
    global IS_LOITER
    global LOITER_POSITION

    if IS_LOITER != True:
        IS_LOITER = True

        # update position
        frame = vehicle.location.global_relative_frame
        LOITER_POSITION = {
            "latitude": frame.lat,
            "longitude": frame.lon
        }

    ignoreVelocityMask =  0b111000
    ignoreAccelMask =  0b111000000
    ignoreYaw = 0b10000000000
    emptyMask = 0b0000000000000000

    msg = vehicle.message_factory.set_position_target_global_int_encode(
        0,       # time_boot_ms (not used)
        0, 0,    # target system, target component
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, # frame
        emptyMask + ignoreAccelMask + ignoreVelocityMask + ignoreYaw, # position and yaw rate enabled
        int(LOITER_POSITION["latitude"] * 1e7), # WGS84 latitude in degrees scaled by 1e7
        int(LOITER_POSITION["longitude"] * 1e7), # WGS84 longitude in degrees scaled by 1e7
        flight_config.settings.altitude,
        0, # X velocity in NED frame in m/s
        0, # Y velocity in NED frame in m/s
        0, # Z velocity in NED frame in m/s
        0, 0, 0, # afx, afy, afz acceleration
        0, math.radians(yawRate))    # yaw, yaw_rate (rad/s)

    vehicle.send_mavlink(msg)


# Historical explanation and inactive implementation below. Python treats this
# quoted block as text; the active setPositionTarget implementation is above.
"""
# The original setPositionTarget function was commented out because it was not working as intended. The new implementation below uses a proportional controller to convert position offsets into velocity commands, which should provide smoother and more responsive control.

# Possible Reasons for Issue:

Looking at your code, I can see the problem. In commands.py, the setPositionTarget function is using position offsets in the body frame (MAV_FRAME_BODY_OFFSET_NED) and sending them at a very high rate (every 0.01 seconds from core.py).

Here's what's happening:

The Problem:
Line 41 in commands.py: The altitude correction is calculated as:

Line 48: The type_mask allows position commands for X, Y, and Z, while ignoring velocities.

The drone is sending position offsets simultaneously for:

Forward/backward movement (to follow you)
Left/right movement (to center on you)
Altitude correction (to maintain 1.75m)
The flight controller is prioritizing horizontal movement over altitude hold, especially when both are commanded as position targets. As the drone follows you, the horizontal movement "wins" and altitude maintenance suffers.

Why This Causes Altitude Loss:
Insufficient thrust margin: When moving horizontally, the drone tilts, reducing vertical thrust. The flight controller may not have enough remaining thrust to simultaneously maintain altitude while tracking the position targets.
Position commands vs velocity commands: Position offsets in body frame can accumulate strangely when sent at high rates (100Hz), causing the altitude controller to lag behind.
No explicit vertical velocity: The code ignores velocity commands entirely (line 52-54 are all zeros), so there's no upward thrust command to actively fight altitude loss.

def setPositionTarget(vehicle: Vehicle, position: Tuple[float, float], yawRate: float) -> None:
    global IS_LOITER

    localNorth, localEast = position

    if localNorth == 0 and localEast == 0:
        # Loiter in place with guided mode
        setLoiterGuided(vehicle, yawRate)
    else:
        IS_LOITER = False

        # Find altitude target for NED frame
        currentAltitude = vehicle.location.global_relative_frame.alt
        targetAltOffset = 0.0 - (flight_config.settings.altitude - currentAltitude) # up is negative

        ignoreVelocityMask =  0b111000
        ignoreAccelMask =  0b111000000
        ignoreYaw = 0b10000000000
        emptyMask = 0b0000000000000000

        msg = vehicle.message_factory.set_position_target_local_ned_encode(
            0,       # time_boot_ms (not used)
            0, 0,    # target system, target component
            mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED, # Use offset from current position
            emptyMask + ignoreAccelMask + ignoreVelocityMask + ignoreYaw, # type_mask
            localNorth, localEast, targetAltOffset,
            0, 0, 0, # x, y, z velocity in m/s (not used)
            0, 0, 0, # x, y, z acceleration (not used)
            0, math.radians(yawRate))    # yaw, yaw_rate

        vehicle.send_mavlink(msg)
"""

"""
# Position-Based Altitude with Velocity Horizontal. This provides even more stable altitude hold by using a hybrid approach of global altitude position + body frame horizontal velocity.

def setPositionTarget(vehicle: Vehicle, position: Tuple[float, float], yawRate: float) -> None:
    global IS_LOITER

    localNorth, localEast = position

    if localNorth == 0 and localEast == 0:
        setLoiterGuided(vehicle, yawRate)
    else:
        IS_LOITER = False

        # Velocities for horizontal movement
        velocityNorth = max(-flight_config.settings.speed, min(flight_config.settings.speed, localNorth * 0.5))
        velocityEast = max(-flight_config.settings.speed, min(flight_config.settings.speed, localEast * 0.5))
        
        # Type mask: ignore XY position, use Z position, ignore velocityZ, ignore accel, ignore yaw
        type_mask = 0b0111000111  # Binary: ignore pos XY, vel Z, accel XYZ, yaw
        
        # Use local NED frame from home position
        msg = vehicle.message_factory.set_position_target_local_ned_encode(
            0, 0, 0,
            mavutil.mavlink.MAV_FRAME_LOCAL_NED,  # Local frame from home
            type_mask,
            0, 0, -flight_config.settings.altitude,  # Position: X,Y ignored, Z is altitude (negative = up in NED)
            velocityNorth, velocityEast, 0,  # Velocities: XY used, Z ignored
            0, 0, 0,
            0, math.radians(yawRate))

        vehicle.send_mavlink(msg)

"""
