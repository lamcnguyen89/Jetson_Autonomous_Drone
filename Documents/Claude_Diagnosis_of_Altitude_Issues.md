# So I have an autonomous drone built with a Jetson Nano, a Herelink Air Unit, Luxonis Depth Camera, and Pixhawk Flight Controller. The drone is supposed to take off in guided mode and follow around the first human it sees. So everything seems to actually work. The Drone takes off and follows me. But the problem is that it seems to be flying at too low of an altitude. It takes off to about the right hieght of 1.75m, but as it follows me , it is like the throttle is not enough and the drone slowly begins to skim the ground as it follows me. Why is this occuring?

Updated todo list

I'll help you diagnose why your drone is losing altitude while following. Let me search through your codebase to understand how the altitude control is implemented.   

Searched codebase for "velocity command altitude control guided mode follow target", 17 results

No matches found. Your search pattern might be excluded completely by either the search.exclude settings or .*ignore files.
If you believe that it should have results, you can check into the .*ignore files and the exclude setting (here are some excluded patterns for reference:[**/node_modules,**/bower_components,**/*.code-search]).
Then if you want to include those files you can call the tool again by setting "includeIgnoredFiles" to true.

Read [](file:///home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/main.py#1-1), lines 1 to 100

Let me look more closely at the command functions and how altitude is handled during following:  

Read [](file:///home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/commands.py)

Read [](file:///home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/rules/follow.py)

## Found the Issue!

Looking at your code, I can see the problem. In commands.py, the `setPositionTarget` function is using **position offsets** in the body frame (`MAV_FRAME_BODY_OFFSET_NED`) and sending them at a very high rate (every 0.01 seconds from core.py).

Here's what's happening:

### The Problem:

1. **Line 41 in commands.py**: The altitude correction is calculated as:
   ```python
   targetAltOffset = 0.0 - (ALTITUDE - currentAltitude)
   ```

2. **Line 48**: The type_mask allows position commands for X, Y, **and Z**, while ignoring velocities.

3. The drone is sending position offsets simultaneously for:
   - Forward/backward movement (to follow you)
   - Left/right movement (to center on you)  
   - Altitude correction (to maintain 1.75m)

4. **The flight controller is prioritizing horizontal movement over altitude hold**, especially when both are commanded as position targets. As the drone follows you, the horizontal movement "wins" and altitude maintenance suffers.

### Why This Causes Altitude Loss:

- **Insufficient thrust margin**: When moving horizontally, the drone tilts, reducing vertical thrust. The flight controller may not have enough remaining thrust to simultaneously maintain altitude while tracking the position targets.
- **Position commands vs velocity commands**: Position offsets in body frame can accumulate strangely when sent at high rates (100Hz), causing the altitude controller to lag behind.
- **No explicit vertical velocity**: The code ignores velocity commands entirely (line 52-54 are all zeros), so there's no upward thrust command to actively fight altitude loss.

### The Solution:

You should **switch to velocity commands** instead of position offsets, or at minimum, keep altitude as a fixed global target rather than a body-relative offset. Here's what I recommend:

**Option 1 (Best)**: Use velocity commands for horizontal movement and position hold for altitude:
- Set the type_mask to ignore X/Y position (bits 0,1) and use X/Y velocity instead
- Keep Z as a position command referenced to the global frame

**Option 2**: Increase the altitude correction aggressiveness by using a larger multiplier or switching to a global altitude reference frame.

**Option 3**: Check your flight controller parameters - you may need to increase throttle hover percentage or tune the altitude hold PID gains.

Would you like me to show you the code changes needed to implement Option 1 (velocity-based control)?

Created 4 todos

# Yes, can you show me velocity based commands for horizontal movement and position hold for alititude?

Updated todo list

I'll show you how to modify the `setPositionTarget` function to use velocity-based horizontal control with position-based altitude hold.

## Recommended Changes to `setPositionTarget`:

```python
def setPositionTarget(vehicle: Vehicle, position: Tuple[float, float], yawRate: float) -> None:
    global IS_LOITER

    localNorth, localEast = position

    if localNorth == 0 and localEast == 0:
        # Loiter in place with guided mode
        setLoiterGuided(vehicle, yawRate)
    else:
        IS_LOITER = False

        # Calculate velocities from position offsets
        # Use a simple proportional controller to convert distance to velocity
        # Cap at SPEED (1.0 m/s from constants)
        velocityNorth = max(-SPEED, min(SPEED, localNorth * 0.5))  # 0.5 is a gain factor
        velocityEast = max(-SPEED, min(SPEED, localEast * 0.5))
        
        # Calculate vertical velocity for altitude correction
        currentAltitude = vehicle.location.global_relative_frame.alt
        altitudeError = ALTITUDE - currentAltitude
        velocityDown = -max(-0.5, min(0.5, altitudeError * 0.8))  # Up is negative, capped at 0.5 m/s
        
        # Type mask: ignore position (bits 0,1,2), ignore acceleration (bits 6,7,8), ignore yaw (bit 10)
        ignorePositionMask = 0b0000111  # Ignore X, Y, Z position
        ignoreAccelMask = 0b111000000
        ignoreYaw = 0b10000000000
        
        type_mask = ignorePositionMask + ignoreAccelMask + ignoreYaw

        msg = vehicle.message_factory.set_position_target_local_ned_encode(
            0,       # time_boot_ms (not used)
            0, 0,    # target system, target component
            mavutil.mavlink.MAV_FRAME_BODY_NED,  # Body frame (not offset)
            type_mask,
            0, 0, 0,  # x, y, z position (not used due to mask)
            velocityNorth, velocityEast, velocityDown,  # velocities in m/s
            0, 0, 0,  # x, y, z acceleration (not used)
            0, math.radians(yawRate))    # yaw, yaw_rate

        vehicle.send_mavlink(msg)
```

## Key Changes Explained:

1. **Velocity Commands (Lines 14-17)**: Converts position offsets to velocities using a proportional gain (0.5). Distance is multiplied by the gain and capped at SPEED (1.0 m/s).

2. **Altitude Velocity Control (Lines 19-22)**: Instead of position offset, calculates a vertical velocity based on altitude error. Positive error (need to climb) results in negative velocity (up is negative in NED). Capped at ±0.5 m/s for smooth control.

3. **Type Mask (Lines 24-28)**: 
   - `0b0000111` ignores X, Y, Z **position** commands (bits 0, 1, 2)
   - Enables X, Y, Z **velocity** commands (bits 3, 4, 5 are NOT set)
   - Still ignores acceleration and yaw position

4. **Frame Change (Line 33)**: Changed from `MAV_FRAME_BODY_OFFSET_NED` to `MAV_FRAME_BODY_NED` since we're using velocities, not position offsets.

## Alternative: Position-Based Altitude with Velocity Horizontal

If you want even more stable altitude hold, you can use a hybrid approach with **global altitude position** + **body-frame horizontal velocities**:

```python
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
```

This second approach gives the flight controller a **hard altitude target** while allowing **smooth velocity-based horizontal following**.

## Tuning Parameters:

- **Horizontal gain (0.5)**: Higher = more aggressive following, lower = smoother
- **Altitude velocity cap (0.5 m/s)**: Adjust based on your drone's vertical performance
- **Altitude gain (0.8)**: How aggressively to correct altitude errors

I recommend starting with the first option (all velocity-based) as it's simpler and should give you better overall flight characteristics. Test in a safe environment first!

Similar code found with 1 license type