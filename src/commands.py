from typing import Tuple
from pymavlink import mavutil
from dronekit import Vehicle
from constants import (
    ALTITUDE,
    ALTITUDE_VELOCITY_GAIN,
    FOLLOW_POSITION_DEADBAND,
    FOLLOW_VELOCITY_GAIN,
    MAX_VERTICAL_SPEED,
    SPEED,
)

import math

IS_LOITER = False
LOITER_POSITION = {
    "latitude": 0,
    "longitude": 0
}

# Restrict a controller output to a safe minimum and maximum value.
def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))

# Convert forward/right position errors in meters into body-frame velocities.
# Small errors are ignored to prevent jitter, and the combined horizontal
# velocity is limited so diagonal movement cannot exceed SPEED.
def positionErrorToHorizontalVelocity(
    forwardError: float,
    rightError: float,
) -> Tuple[float, float]:
    if abs(forwardError) <= FOLLOW_POSITION_DEADBAND:
        forwardError = 0.0
    if abs(rightError) <= FOLLOW_POSITION_DEADBAND:
        rightError = 0.0

    forwardVelocity = forwardError * FOLLOW_VELOCITY_GAIN
    rightVelocity = rightError * FOLLOW_VELOCITY_GAIN

    # Limit total horizontal speed, including diagonal movement.
    magnitude = math.hypot(forwardVelocity, rightVelocity)
    if magnitude > SPEED:
        scale = SPEED / magnitude
        forwardVelocity *= scale
        rightVelocity *= scale

    return forwardVelocity, rightVelocity

# Convert home-relative altitude error into an NED vertical velocity.
# NED is positive downward, so being below ALTITUDE produces a negative
# velocity (climb); the result is capped for a controlled correction.
def altitudeErrorToVerticalVelocity(currentAltitude: float) -> float:
    # MAVLink NED uses positive Z velocity for down and negative for up.
    verticalVelocity = (
        currentAltitude - ALTITUDE
    ) * ALTITUDE_VELOCITY_GAIN
    return clamp(
        verticalVelocity,
        -MAX_VERTICAL_SPEED,
        MAX_VERTICAL_SPEED,
    )

def setYaw(vehicle: Vehicle, relativeYaw: float) -> None:
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
    global IS_LOITER

    forwardError, rightError = position

    if forwardError == 0 and rightError == 0:
        # Loiter in place with guided mode
        setLoiterGuided(vehicle, yawRate)
    else:
        IS_LOITER = False

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
        emptyMask + ignoreAccelMask + ignoreVelocityMask + ignoreYaw, # type_mask (only speeds enabled)
        int(LOITER_POSITION["latitude"] * 1e7), # lat_int - X Position in WGS84 frame in 1e7 * meters
        int(LOITER_POSITION["longitude"] * 1e7), # lon_int - Y Position in WGS84 frame in 1e7 * meters
        ALTITUDE,
        0, # X velocity in NED frame in m/s
        0, # Y velocity in NED frame in m/s
        0, # Z velocity in NED frame in m/s
        0, 0, 0, # afx, afy, afz acceleration
        0, math.radians(yawRate))    # yaw, yaw_rate (rad/s)

    vehicle.send_mavlink(msg)


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
        targetAltOffset = 0.0 - (ALTITUDE - currentAltitude) # up is negative

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
        velocityNorth = max(-SPEED, min(SPEED, localNorth * 0.5))
        velocityEast = max(-SPEED, min(SPEED, localEast * 0.5))
        
        # Type mask: ignore XY position, use Z position, ignore velocityZ, ignore accel, ignore yaw
        type_mask = 0b0111000111  # Binary: ignore pos XY, vel Z, accel XYZ, yaw
        
        # Use local NED frame from home position
        msg = vehicle.message_factory.set_position_target_local_ned_encode(
            0, 0, 0,
            mavutil.mavlink.MAV_FRAME_LOCAL_NED,  # Local frame from home
            type_mask,
            0, 0, -ALTITUDE,  # Position: X,Y ignored, Z is altitude (negative = up in NED)
            velocityNorth, velocityEast, 0,  # Velocities: XY used, Z ignored
            0, 0, 0,
            0, math.radians(yawRate))

        vehicle.send_mavlink(msg)

"""