# So I have an autonomous drone built with a Jetson Nano, a Herelink Air Unit, Luxonis Depth Camera, and Pixhawk Flight Controller. The drone is supposed to take off in guided mode and follow around the first human it sees. So everything seems to actually work. The Drone takes off and follows me. But the problem is that it seems to be flying at too low of an altitude. It takes off to about the right hieght of 1.75m, but as it follows me , it is like the throttle is not enough and the drone slowly begins to skim the ground as it follows me. Why is this occuring?

The Jetson code is not directly commanding throttle. It commands a position and altitude; the Pixhawk decides how much throttle is needed. Your code does request approximately 1.75 m during following, so the descent is more likely caused by altitude sensing, aggressive horizontal movement, or insufficient thrust headroom—not an intentionally low throttle command.

A few specific issues stand out:

- `ALTITUDE = 1.75` is altitude relative to the home/arming point, not height above the ground. The code even notes this limitation in [core.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/core.py:93). Walking uphill will therefore reduce ground clearance.
- The Luxonis camera is only being used to locate the person. It is not providing downward ground clearance to the Pixhawk.
- The follow controller sends raw distance error as a position offset in [follow.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/rules/follow.py:29), repeating it about 100 times per second. The `SPEED = 1.0` setting is never actually used. This can make the drone accelerate and tilt more aggressively than expected.
- Once following begins, there is no minimum-altitude safety check. `isAltitudeOk()` is only used before entering the running state.
- Your latest successful log later reports `Battery 1 is low 21.15V` followed by `Battery Failsafe` in [logs/10.log](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/logs/10.log:383). That does not prove the earlier descent was battery-related, but it makes limited thrust headroom a serious possibility.

At only 1.75 m, barometer errors are also plausible. ArduPilot specifically warns that prop wash near the ground can disturb altitude holding, and forward flight can alter pressure around the autopilot enough to make it believe it is climbing—causing it to descend. It also warns that aircraft hovering above roughly 70% throttle may not have enough power to maintain altitude while tilted. [ArduPilot AltHold documentation](https://ardupilot.org/copter/docs/altholdmode.html)

The decisive diagnostic is the Pixhawk’s onboard `.bin` flight log. Plot:

- `CTUN.DAlt`: desired altitude
- `CTUN.Alt`: EKF-estimated altitude
- `CTUN.BAlt`: barometer altitude
- `CTUN.ThO` and `CTUN.ThH`: throttle output and estimated hover throttle
- battery voltage and motor outputs

Interpret it like this:

| Log behavior | Likely cause |
|---|---|
| `DAlt` stays near 1.75 m, `Alt` drops, throttle rises/saturates | Battery, motors, props, weight, or altitude-controller tuning |
| `DAlt` and `Alt` remain steady but the drone physically descends | Barometer/altitude-estimation error |
| `DAlt` itself moves downward | MAVLink setpoint/frame problem |
| Loiter also loses altitude during horizontal motion | Pixhawk sensing, tuning, or propulsion—not person-following code |
| Only the application loses altitude | Follow-controller/setpoint implementation |

Before another autonomous test, use a fully charged battery and test AltHold/Loiter horizontal movement at a safer altitude with a safety pilot. Longer-term, I would add a downward-facing rangefinder connected/configured as a Pixhawk rangefinder, limit follow speed and acceleration explicitly, and add a hard low-altitude abort. ArduPilot explains that a rangefinder can provide substantially better low-altitude ground tracking. [Understanding altitude in ArduPilot](https://ardupilot.org/copter/docs/common-understanding-altitude.html)

This is separate from the earlier Herelink heartbeat race: that problem prevented dependable takeoff/state handling, whereas your current logs show the application successfully entering `RUNNING/follow`.

# So I was talking to ChatGPT. It said that a solution would be to use velocity instead of position commands for the setPositionTarget function

Yes—velocity commands are a better fit for person-following, but they will not automatically fix altitude loss.

Your current code repeatedly commands a position several metres ahead of the drone:

```python
self._targetPosition = (
    zDistance - MINIMUM_DISTANCE,
    xDistance
)
```

Because that offset is recalculated about 100 times per second, the destination keeps moving along with the drone. It behaves like a continually advancing “carrot,” potentially causing more aggressive acceleration and tilt than intended. Also, the `SPEED = 1.0` constant is currently unused.

A better controller would translate tracking error into bounded velocity:

```text
forward velocity = distance error × gain
sideways velocity = horizontal error × gain
vertical velocity = altitude error × gain
```

For the NED coordinate system, positive vertical velocity is downward, so:

```python
vz = current_altitude - ALTITUDE
```

- Below 1.75 m → negative `vz` → climb
- Above 1.75 m → positive `vz` → descend

All velocities should be limited, for example:

```python
vx = clamp(distance_error * 0.4, -1.0, 1.0)
vy = clamp(horizontal_error * 0.4, -0.6, 0.6)
vz = clamp((current_altitude - ALTITUDE) * 0.8, -0.5, 0.5)
```

Then send `SET_POSITION_TARGET_LOCAL_NED` using:

- `MAV_FRAME_BODY_OFFSET_NED`
- velocity fields `vx`, `vy`, and `vz`
- position and acceleration fields ignored
- yaw-rate enabled
- updates around 10–20 Hz rather than 100 Hz

This would give you explicit control over follow speed and prevent a large person-distance measurement from producing an aggressive position target. ArduPilot documents body-frame velocity targets for Guided mode and requires them to be resent because they time out after several seconds. [ArduPilot Guided commands](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html)

However, there is an important limitation: this still uses altitude above home, not actual ground clearance. It also relies on the Jetson’s delayed altitude telemetry to generate vertical velocity. Therefore I would combine the velocity conversion with:

- A downward-facing Pixhawk rangefinder for low-altitude surface tracking.
- A hard low-altitude safety response.
- A deadband so noisy detections do not cause constant motion.
- A fully charged battery and adequate hover-throttle margin.
- Testing at a safer altitude before returning to 1.75 m.

So ChatGPT’s recommendation is directionally correct: velocity commands are more appropriate for following. But if the Pixhawk’s barometer is being disturbed or the propulsion system lacks thrust while tilted, converting commands alone will not solve the descent.

# Can you add improvements to the setTargetPosition Function?

Implemented the velocity-based improvements in [commands.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/commands.py:146).

Changes include:

- Converts forward/sideways position error into body-frame velocity.
- Limits total horizontal speed to `1.0 m/s`, including diagonal movement.
- Adds a `0.15 m` deadband to reduce detection jitter.
- Uses vertical velocity feedback to maintain 1.75 m.
- Limits climb/descent correction to `0.5 m/s`.
- Uses the correct NED sign: negative vertical velocity climbs.
- Sends MAVLink velocity plus yaw-rate mask `1479`.
- Preserves the existing position-based loiter behavior when no movement is requested.
- Adds configurable gains in [constants.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/constants.py:8).
- Adds focused tests in [test_commands.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Aurelia_Herelink_Autonomous_Drone/src/rules/tests/test_commands.py:1).

Verification:

- 5 command-controller tests passed.
- Syntax and diff checks passed.
- The no-drift SITL flight test passed, although its existing teardown threads required interruption after pytest reported success.

Start flight testing with a fully charged battery, a safety pilot, and more altitude than 1.75 m. This controller should produce gentler movement, but it cannot compensate for inadequate propulsion or incorrect barometer readings.