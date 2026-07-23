The evidence points to a MAVLink/DroneKit state race—not Guided mode itself.

### Primary cause: extra heartbeats overwrite the armed state

The logs contain thousands of invalid heartbeats with:

- `custom_mode=0, base_mode=0`
- `custom_mode=33554829, base_mode=0`

For example: [logs/2.log](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:8).

DroneKit’s heartbeat handler sets `_armed` from every non-GCS heartbeat before checking whether that heartbeat belongs to a valid vehicle: [DroneKit handler](/home/malneyugnfl/.local/lib/python3.9/site-packages/dronekit/__init__.py:1213). A peripheral or badly routed MAVLink stream is therefore repeatedly telling DroneKit “disarmed.”

That triggers your callback, which resets the state machine to `AWAITING_ARM` whenever `vehicle.armed` becomes false: [core.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/src/core.py:73).

A representative sequence is:

1. Guided detected at [logs/2.log:584](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:584).
2. Takeoff process starts five seconds later at [logs/2.log:667](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:667).
3. Vehicle reports armed at [logs/2.log:726](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:726).
4. Takeoff is sent.
5. Roughly half a second later, another heartbeat reports disarmed at [logs/2.log:729](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:729), resetting the process.

Because your arming loop only samples once per second, it sometimes catches the brief “armed” interval and sometimes misses it. That explains the random immediate, delayed, and never-takeoff behavior.

### Other confirmed blockers

- The autopilot repeatedly rejects arming because RC sticks are not neutral—especially yaw, throttle, and occasionally roll. Examples: [logs/1.log:594](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/1.log:594) and [logs/1.log:1042](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/1.log:1042). Your code retries forever without surfacing or timing out on the rejection.
- One run has no heartbeat whatsoever and never initializes: [logs/3.log:6](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/3.log:6).
- There are also serial framing/CRC errors: [logs/2.log:5](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:5).
- Multiple flights report `Potential Thrust Loss`. Treat that as a separate safety-critical problem before further prop-on testing: [logs/1.log:1171](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/1.log:1171).

### What I would check first

1. Log each heartbeat’s `sysid`, `compid`, vehicle `type`, `autopilot`, `base_mode`, and `custom_mode`. Then ensure only the actual flight-controller heartbeat—normally autopilot component 1—updates DroneKit’s mode and armed state.
2. Ensure only one process directly reads `/dev/ttyTHS1`. In particular, don’t run MAVProxy and the always-restarting `stanley.service` against the serial device simultaneously; use MAVLink routing/UDP if both need telemetry.
3. Confirm the Pixhawk serial port and Jetson both use 57600 baud, and check wiring/common ground.
4. Recalibrate RC trims/deadzones so RC1/RC3/RC4 are genuinely neutral before autonomous arming.
5. Add an arming timeout and report `STATUSTEXT`/command rejection instead of endlessly retrying.

There is also a risky behavior in [core.py:68](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/src/core.py:68): switching into Guided while already armed causes the application to issue a disarm command. That should be reconsidered before flight testing.

# I asked: So the thrust loss errors are because I did not have the propellors attached. And the RC sticks not neutral are because of me. So what do you think is the main issue?

The main issue is that DroneKit is processing extra MAVLink heartbeats as though they came from the flight controller.

Those heartbeats report `base_mode=0`, so DroneKit repeatedly changes `vehicle.armed` to `False`. Your `armedCallback()` then resets the state machine to `AWAITING_ARM`. This creates a timing race:

- Pixhawk heartbeat says armed.
- Your code may begin takeoff.
- Another component’s heartbeat says disarmed about half a second later.
- The state machine restarts.
- Sometimes the one-second polling loop catches the armed window; sometimes it doesn’t.

That precisely matches the inconsistent behavior.

The likely underlying source is one or more additional MAVLink components on the same stream. Less likely, but possible, is a routing/serial corruption problem. The next decisive step is to log every heartbeat’s `sysid`, `compid`, `type`, `autopilot`, `base_mode`, and `custom_mode`. The invalid heartbeats can then be identified and excluded from vehicle-state processing.

There is also a code-level weakness: [core.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/src/core.py:73) trusts every change to DroneKit’s shared `armed` attribute. Until the heartbeats are filtered, no amount of takeoff retry logic will make this dependable.

The complete no-heartbeat run is a separate serial/link issue, potentially caused by another program—such as MAVProxy—reading `/dev/ttyTHS1` simultaneously. But for the intermittent takeoff behavior in the successful connections, heartbeat contamination is the main issue.

# I asked if the Herelink controller is possibly causing Heartbeat contamination that is causing race conditions with the drone code. This is what it said:

Yes—Herelink is very likely the source, but not because it uses 2.4 GHz.

The probable chain is:

```text
Herelink heartbeat
        ↓
Pixhawk MAVLink routing
        ↓
Jetson telemetry port
        ↓
DroneKit mistakes it for the autopilot heartbeat
        ↓
vehicle.armed becomes False
        ↓
Your state machine restarts
```

Herelink carries MAVLink telemetry over its UART, and ArduPilot normally forwards MAVLink components—including heartbeats—between telemetry ports. [CubePilot documentation](https://docs.cubepilot.org/user-guides/herelink/herelink-overview), [ArduPilot routing documentation](https://ardupilot.org/dev/docs/mavlink-routing-in-ardupilot.html).

More convincingly, another Herelink/DroneKit setup reported the exact same two errors as yours: `mode (33554829, 0)` and `mode (0, 0)`. [Herelink/DroneKit report](https://discuss.cubepilot.org/t/connect-to-drone-via-herelink-and-drone-kit-python/7887)

RF interference would generally produce missing packets, CRC errors, and timeouts—not thousands of consistently decoded heartbeats with the same values.

The quickest confirmation is to bench-test with the Herelink telemetry UART disconnected from the Pixhawk. If those two heartbeat errors and the `armed 1 → armed 0` oscillation disappear, it is confirmed.

The preferred fix is to make the Jetson accept armed/mode state only from the flight controller’s heartbeat, normally:

- Flight-controller system ID, commonly `sysid=1`
- Autopilot component, normally `compid=1`

A configuration alternative on ArduPilot 4.6 is disabling MAVLink forwarding on the Jetson’s particular telemetry channel using bit 10 of that port’s `SERIALn_OPTIONS`. Code-side filtering is safer and more explicit because it preserves useful MAVLink routing for other components.

# I asked: Can you explain the solution more clearly? I am new to this. So Herelink is likely the source of the issues, how can I fix this without having to removing the Herelink system?

You can keep Herelink. It is probably behaving normally; the problem is that the older DroneKit library mistakes Herelink’s heartbeat for the Pixhawk’s heartbeat.

The simplest fix is to prevent Herelink’s MAVLink messages from being forwarded to the Jetson telemetry port.

## Recommended fix: isolate the Jetson telemetry port

Your connections are probably similar to:

```text
Herelink ── TELEM1 ── Pixhawk ── TELEM2 ── Jetson
```

ArduPilot normally forwards messages between TELEM1 and TELEM2. We want the Jetson to communicate directly with the Pixhawk without receiving Herelink’s additional heartbeats.

### 1. Identify the Jetson’s Pixhawk port

Follow the Jetson’s TX/RX cable to the Pixhawk:

- Pixhawk `TELEM1` normally corresponds to `SERIAL1`
- Pixhawk `TELEM2` normally corresponds to `SERIAL2`

The `/dev/ttyTHS1` name only identifies the Jetson side; it does not tell us which Pixhawk port is used.

### 2. Change the options for that port

In Mission Planner:

1. Connect through Herelink.
2. Open **Config → Full Parameter Tree** or **Full Parameter List**.
3. Find the options parameter for the Jetson port:
   - Jetson on TELEM1: `SERIAL1_OPTIONS`
   - Jetson on TELEM2: `SERIAL2_OPTIONS`
4. Record its current value.
5. Enable option **bit 10: “Don’t forward MAVLink to/from this channel.”**
6. Reboot the Pixhawk.

For ArduCopter 4.6.3, bit 10 has the numerical value `1024`.

If the current value is `0`, set it to:

```text
1024
```

If it already contains options, do not simply replace it. The new value must be:

```text
current value + 1024
```

This assumes bit 10 was not already enabled. Mission Planner may provide checkboxes, which is safer than calculating the bitmask manually.

Do not change `SERIALn_PROTOCOL` or `SERIALn_BAUD`.

This setting should still allow:

- Herelink to control the Pixhawk.
- Herelink to receive Pixhawk telemetry.
- Jetson to send Guided commands directly to the Pixhawk.
- Jetson to receive telemetry generated by the Pixhawk.

It only prevents messages belonging to other MAVLink devices from being routed through the Jetson channel. ArduPilot documents this option as “Don’t forward mavlink to/from this channel.” [ArduPilot serial options](https://ardupilot.org/plane/docs/common-serial-options.html)

## Verify the result

After rebooting, run the application with the propellers removed.

The following errors should disappear:

```text
mode (33554829, 0) not available
mode (0, 0) not available
```

You should also stop seeing this repeated pattern:

```text
Vehicle armed 1
Vehicle armed 0
Vehicle armed 1
Vehicle armed 0
```

When the Pixhawk arms, `Vehicle armed 1` should remain stable until it actually disarms.

## Alternative: filter heartbeats in software

A more robust long-term solution is to modify the application so DroneKit accepts state only from the Pixhawk:

```text
Pixhawk system ID: the value of SYSID_THISMAV, usually 1
Pixhawk component ID: 1
```

Heartbeats from Herelink, cameras, gimbals, or other components would be ignored for `mode` and `armed` state.

This requires filtering before DroneKit’s built-in heartbeat handler runs; merely adding another heartbeat listener in `main.py` will not fix it. The clean approach is a small custom `Vehicle` subclass passed to `connect()`.

I recommend trying the `SERIALn_OPTIONS` change first because it is simpler and does not require altering the installed DroneKit package. If you want, I can also implement the software filter in this project so it remains protected even when MAVLink routing is enabled.


# I asked: Sowhat you are trying to say is that I need to find a way to prevent the herelink heartbeat from reaching the Jetson. The Jetson should only be receiving signals from the Pixhawk Flight Controller. So the Pixhawk is forwarding signals from the Herelink to the Jetson?

Yes, that is essentially correct—with one important distinction: these are MAVLink messages, not the Herelink’s 2.4 GHz radio signal itself.

The communication path is likely:

```text
Herelink
   │ MAVLink commands and heartbeats
   ▼
Pixhawk TELEM1
   │
   ├── Pixhawk processes Herelink commands
   │
   └── Pixhawk forwards some messages to other MAVLink ports
                                               │
                                               ▼
                                      TELEM2 → Jetson
```

The Pixhawk supports MAVLink routing, so it forwards heartbeats from Herelink components—possibly its camera, system controller, or ground-station software—to the Jetson.

That routing is normally useful. The actual bug is that DroneKit treats those additional heartbeats as if they describe the Pixhawk. When a Herelink component sends a heartbeat with `base_mode=0`, DroneKit incorrectly concludes that the drone is disarmed.

The desired behavior is:

```text
Pixhawk heartbeat → controls DroneKit's mode and armed state
Herelink heartbeat → ignored by DroneKit
```

You have two ways to accomplish that:

1. Disable MAVLink forwarding on the Pixhawk telemetry port connected to the Jetson.
2. Keep routing enabled, but change the Jetson software to accept mode/armed information only from the Pixhawk’s `sysid` and `compid`.

The second solution is more precise because the Jetson can continue receiving useful messages from other components while ignoring their heartbeats for vehicle state. The first solution is simpler to configure.

Either way, Herelink continues controlling the Pixhawk normally. The Pixhawk processes Herelink’s commands locally; it just stops passing the unwanted component heartbeats to DroneKit, or DroneKit learns to ignore them.

# I Asked: Ok. So I will implement Solution 2. But are you certain that this heartbeat contamination from ther Herelink is the issue? I'm pretty sure there is some sort of race condition because race conditions tend to cause unpredictable errors like I've been having.

Yes, there is a race condition. The evidence strongly indicates that the extra heartbeat is causing it.

What the logs prove:

1. The Pixhawk heartbeat reports armed:

```text
22:23:12.122  Vehicle armed 1
```

2. Your program sends takeoff two milliseconds later:

```text
22:23:12.124  Take off to 1.75m
```

3. An invalid heartbeat arrives 568 milliseconds later:

```text
22:23:12.692  Vehicle armed 0
22:23:12.692  mode (33554829, 0) not available
```

See [logs/2.log](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/2.log:726).

DroneKit updates `vehicle.armed` before validating which component sent the heartbeat. Your callback then immediately resets the state machine when it sees `False`: [core.py](/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/src/core.py:73).

Because your arming loop checks only once per second, it may:

- Catch the brief `armed=True` window and send takeoff.
- Miss that window and restart.
- Send takeoff, then get reset immediately afterward.

That is the race and explains the unpredictability extremely well.

What is not yet proven with 100% certainty is that Herelink sent the contaminating heartbeat. The logs currently omit the heartbeat’s source system and component IDs. Herelink is the leading candidate because of your topology and matching reports, but it could theoretically be another MAVLink component.

Before applying the filter, I recommend one diagnostic run that logs:

```text
sysid
compid
type
autopilot
base_mode
custom_mode
```

You should see something resembling:

```text
sysid=1  compid=1  autopilot=ArduPilot  → Pixhawk
sysid=42 compid=... base_mode=0         → likely Herelink component
```

Then Solution 2 should accept mode and armed state only when:

```text
sysid == SYSID_THISMAV
compid == 1
autopilot == MAV_AUTOPILOT_ARDUPILOTMEGA
```

Even if the contaminating heartbeat turns out to be a camera, gimbal, or another component instead of Herelink, this filter remains the correct solution. It makes the Jetson trust only the actual flight controller. My confidence that heartbeat contamination is driving the race is very high; identifying Herelink specifically requires capturing those source IDs.

# I asked: So how would I implement solution 2 in the code? Or do I need to go to Mission Planner to do this

Solution 2 is implemented entirely in the Jetson code. You do not need Mission Planner, although it can help confirm `SYSID_THISMAV`.

The clean approach is to create a custom DroneKit `Vehicle` that discards heartbeats not sent by an ArduPilot flight-controller component. Do not edit the installed DroneKit package directly.

### 1. Create `src/filtered_vehicle.py`

```python
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
```

The important part is the `return`. It rejects the unwanted heartbeat before DroneKit can change `vehicle.armed`.

### 2. Import the class in `src/main.py`

Add:

```python
from filtered_vehicle import FlightControllerVehicle
```

### 3. Pass it to `connect()`

Change the connection from:

```python
vehicle = connect(
    args.uri,
    baud=baud_rate,
    wait_ready=['gps_0', 'armed', 'mode', 'attitude'],
    rate=20
)
```

to:

```python
vehicle = connect(
    args.uri,
    baud=baud_rate,
    wait_ready=['gps_0', 'armed', 'mode', 'attitude'],
    rate=20,
    vehicle_class=FlightControllerVehicle,
)
```

Do not use `source_system` as a substitute. That setting changes the Jetson’s outgoing MAVLink identity; it does not filter incoming heartbeats.

### 4. Bench-test it

Test without propellers. On startup, the log should show something like:

```text
Trusting flight controller heartbeat: sysid=1 compid=1
Ignoring non-flight-controller heartbeat: sysid=42 compid=...
```

The existing errors should disappear:

```text
mode (33554829, 0) not available
mode (0, 0) not available
```

After arming, `Vehicle armed 1` should remain stable instead of repeatedly changing back to `Vehicle armed 0`.

If the application cannot connect after this change, the heartbeat fields differ from what we expect. The new warning messages will help identify the actual values. Mission Planner is only needed if you want to verify that the Pixhawk’s `SYSID_THISMAV` agrees with the trusted `sysid` shown in the log.