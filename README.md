# Jetson_Aurelia_Herelink_Autonomous_Drone
Code for Autonomous Drone using Jetson Orin Nano, the Herelink Video/RC System, and Luxonis Depth Camera

## Installation and systemd startup

The installer targets Ubuntu/Debian with systemd (the Jetson uses Ubuntu 22.04).
It installs native packages with APT and Python packages into `.venv` in the
project root. Python 3.9-3.12 is supported; Ubuntu 22.04's Python 3.10 works.
Python packages and the pip cache are owned by `SERVICE_USER`, not root.

Keep machine settings in `.env` in the project root (ignored by Git). On a new
checkout, the installer creates it from `.env.example` if missing and replaces
the example `SERVICE_USER` placeholder with the account invoking `sudo` (or the
checkout owner when run directly as root). The new file is owned by that account.
An existing `.env` is preserved, including its contents and permissions. You can
also copy the example yourself before installing to customize these settings:

- `SERVICE_USER`: the Linux account that runs the drone. It needs write access to
  the repository and read access to `.env`.
- `BASE_PYTHON`: the absolute interpreter used to create `.venv`, normally
  `/usr/bin/python3`. This replaces the previous `PYTHON_BIN` setting. The service
  always runs the project's `.venv/bin/python`.
- `MAVLINK_PORT`: the controller's USB serial device. Prefer an entry shown by
  `ls -l /dev/serial/by-id/`; `/dev/ttyACM0` is an example, not an auto-detected port.
  An onboard UART such as `/dev/ttyTHS1` is also supported. Connect the selected
  controller before running the installer; it verifies that the port exists and
  identifies a tty device.

Use literal values in `.env`, quoting values with spaces. Do not use `export`,
variable references, or shell commands: Python, Bash and systemd read this file.
Keep comments on separate lines so all readers interpret the settings consistently.

From the project root, install the flight runtime and enable startup at boot:

```bash
sudo ./scripts/Install_Auto_Drone.sh
```

The script discovers the project location, so an absolute path to the installer
also works from another working directory. It validates dependencies and the
rendered service before installing `/etc/systemd/system/Auto_Drone.service`.
Add `--start` to also start/restart it immediately. Stop and disable any older
drone service before running this one, so only one process accesses the controller.
On subsequent installs, an active service keeps running until restarted; use
`--start` when ready to apply dependency changes to that process.

For simulation and tests, include the optional Python dependencies:

```bash
sudo ./scripts/Install_Auto_Drone.sh --with-sitl
.venv/bin/python -m pytest src/rules/tests/test_commands.py
```

`--with-sitl` installs simulation libraries; it does not build an ArduPilot SITL
executable. Integration tests also need a Linux executable for the host's CPU
architecture in `simulation/ardupilot/arducopter-linux`, or an
`ARDUPILOT_SITL_BINARY` override.
The optional Vue visualiser has separate Node dependencies in
`simulation/visualiser/package.json`; install those with `yarn install` in that
directory when developing the UI. They are not needed by the flight service.

All SITL tooling and assets live in `simulation/`. From the project root:

```bash
.venv/bin/python -m simulation.sitl  # Interactive backend
./simulation/integration.sh        # SITL integration tests
```

See [simulation/README.md](simulation/README.md) for the layout, frontend commands,
and running tools from other directories. The flight service continues to use
`src/main.py`.

## Flight configuration

Edit the flight settings in the project root `.env`. All 13 values formerly in
`src/constants.py` are now documented in `.env.example`, with the original values
preserved. There are no numeric defaults hidden in Python code.
The serial baud rate formerly in `src/main.py` is configured here as well.

| Settings | Meaning |
| --- | --- |
| `BAUD_RATE` | Serial MAVLink baud rate; a positive integer matching the flight controller |
| `ALTITUDE`, `ALTITUDE_FUZZINESS` | Target altitude above home and allowed error, in meters |
| `MINIMUM_DISTANCE`, `BACKOFF_DISTANCE` | Person-following distance and backoff trigger, in meters |
| `HEARTBEAT_TIMEOUT` | Maximum flight-controller heartbeat age, in seconds |
| `YAW_RATE` | Follow/search yaw rate limit, in degrees/second |
| `SPEED`, `MAX_VERTICAL_SPEED` | Horizontal and vertical speed limits, in meters/second |
| `FOLLOW_VELOCITY_GAIN`, `ALTITUDE_VELOCITY_GAIN` | Position-error to velocity gains, in 1/second |
| `FOLLOW_POSITION_DEADBAND` | Horizontal position errors ignored, in meters |
| `DETECTION_THRESH` | Minimum camera detection confidence, from 0 to 1 |
| `YAW_MAX_DAMP` | Preserved setting; currently unused by the movement rules |

`src/config.py` replaces the old constants module. It reads the root `.env`
using an absolute path derived from its own location, so direct runs and SITL
work from other directories. Explicit process environment variables override
file values; systemd already passes the same file through `EnvironmentFile=`.
The loader does not modify the process environment or interpolate `$variables`.

Settings are converted to finite numbers and checked at startup. Missing,
nonnumeric or negative values cause an error. `BAUD_RATE` must be a positive
integer. Altitude, following distance and
heartbeat timeout must be positive; altitude tolerance must be below the target
altitude, the backoff trigger must not exceed the following distance, and
detection confidence must be between 0 and 1.

The shared settings object is immutable for the process lifetime. After changing
flight values, restart the application, SITL backend, or service:

```bash
sudo systemctl restart Auto_Drone.service
```

Movement-only edits need no service reinstall or daemon reload. The installer
checks the flight configuration as part of its application import check before
installing/enabling the service. Simulation-only camera settings remain in
`simulation/constants.py`.

## Log retention

On every service process startup (including automatic restarts), the service
scans previous `.log` files in `--log_path` (the project `logs/` directory by
default) and deletes those without a recorded arming attempt. Cleanup runs once,
before opening the new session's log, so the current log stays available until
the next startup even if the flight controller cannot connect.

Logs are retained when they contain a reported armed state (`Vehicle armed 1`),
an arm rejection (`Arm: ...`), `Arming motors`, or takeoff activity. Legacy
`Waiting for arming...` logs and flight-controller acknowledgements of
`MAV_CMD_COMPONENT_ARM_DISARM` are retained too (an acknowledgement alone cannot
distinguish an arm request from a disarm request). Waiting in `AWAITING_ARM`, routine
`PreArm:` warnings, and connection tracebacks do not count as arming attempts.
Unreadable logs are retained with a warning. Other file types, subdirectories,
and symlinks are left alone. New log numbers follow the highest retained number
to avoid appending to a retained flight log after cleanup leaves numbering gaps.

## Debugging logs

Each log line includes a timestamp, severity, logger name, and thread name.
Startup records the selected connection, baud rate, process ID, log path, and
flight/notification settings. Connection readiness, camera readiness, shutdown,
and exceptions in startup or the control thread are recorded too.

- Notifications record the requested text/beep count and enable switches. Each
  `STATUSTEXT send attempt` includes severity and text; `STATUSTEXT queued` confirms
  that the local MAVLink queue accepted it. Queue failures include tracebacks.
  Queue success does not confirm reception or display on Herelink. Buzzer send
  attempts and telemetry stream requests are also logged. Custom notifications
  skipped during cooldown and rule announcements deferred by cooldown have DEBUG
  entries; repeated control cycles do not repeat the same deferred-rule entry.
- Every winning tracking-rule change records the old/new rule and its initial
  position error and yaw rate, independently of notification settings/cooldown.
  Core state transitions record their reason. Arming, mode changes, heartbeat
  timeouts, takeoff delay/cancellation, takeoff commands, and shutdown flight
  commands are recorded without logging each movement command.
- Incoming `STATUSTEXT` (including pre-arm failures and flight-controller warnings)
  is recorded once by DroneKit's `autopilot` logger at its original severity.
  Trusted flight-controller `COMMAND_ACK` messages include source IDs, command and
  result names/numbers, progress, and result details; rejected commands use WARNING.
  Heartbeat mode/armed/system-status, sensor-health masks, GPS fix type, and EKF
  flags are logged on their first sample and when they change. Unchanged telemetry
  packets are omitted. GPS entries include satellite count; sensor-health entries
  include battery voltage/remaining capacity at that moment.

The service already logs at DEBUG level. To watch its journal during startup:

```bash
journalctl -u Auto_Drone.service -f
```

The existing retention policy still removes sessions without an arming attempt on
the next startup; copy a startup-only log elsewhere if you need to keep it.

## Arming and autonomous takeoff

Select GUIDED mode, then press your normal arm button, using the same arming
procedure as manual flight. The flight controller must accept the arm request;
its normal pre-arm checks, including any low-throttle requirement, still apply.
The tracking service never sends an arm command, so selecting GUIDED or lowering
the throttle alone will not arm the drone.

Once the controller reports armed, the camera is running, and the connection is
healthy, the service waits five seconds before commanding takeoff to `ALTITUDE`.
Disarming, leaving GUIDED, losing the connection, or stopping the camera prevents
takeoff. After a disarm, use the arm button again to start another takeoff.

## Status text and buzzer notifications

The flight service shares one `Notifier` (`src/notifications.py`) between startup
and `Core`. It queues MAVLink packets through the existing DroneKit connection.

Announcements use MAVLink `NOTICE` severity (5), which QGroundControl reads aloud
when audio is enabled. On Herelink running QGroundControl, open Application
Settings > General > Miscellaneous and clear **Mute all audio output**. Turn up
the controller's Android media volume, then release and press RC9 to request
`Person Tracking Ready`. Speech is generated by the controller's text-to-speech
engine; it is separate from the aircraft buzzer. If text appears but speech is
silent, check the controller's text-to-speech engine/voice and whether other
QGroundControl announcements speak. Other ground-station apps may handle speech
differently. The speech threshold is implemented in
[QGroundControl's status-text handler](https://github.com/mavlink/qgroundcontrol/blob/v4.2.8/src/Vehicle/Vehicle.cc#L838-L852).
Restart the flight service after updating the code to apply the new severity.

| Event | Status text | Default beeps |
| --- | --- | --- |
| Service ready | `Human Tracking Service Ready` | 6 |
| Autonomous takeoff after the five-second delay | `Human Tracking Active` | 2 |
| RC9 enters PWM 1600-2000 | `Person Tracking Ready` | 3 |
| RC12 wheel enters high end (PWM >=1800) | `RC12 Wheel High` | 2 |
| RC12 wheel enters low end (PWM <=1200) | `RC12 Wheel Low` | 2 |
| Backoff | `Tracking: Backoff - person too close` | 3 |
| Follow | `Tracking: Following person` | 1 |
| Search | `Tracking: Target lost - searching` | 2 |
| None | `Tracking: Waiting for person` | 0 |

Readiness is announced once per successful service startup, after the camera's
first output, a recent flight-controller heartbeat, and `Core` initialization,
before starting the control thread. An empty detection batch is sufficient; no
person, arming, takeoff or GUIDED mode is required for this announcement. Startup
cancellation, an active startup killswitch, camera failure, or camera timeout
prevents it. Camera startup waits are bounded and cancelled pipelines can stop
without blocking forever on an empty output queue.

Each autonomous takeoff requests two beeps and `Human Tracking Active` after the
five-second delay and final takeoff checks. A cancelled takeoff does not announce
activation. Activation bypasses the notification cooldown, then starts a new
cooldown so a rule announcement does not immediately replace it. The existing
text and buzzer enable switches apply to this announcement too.

After service readiness, the RC9 button can test the notification path in any
flight mode, including while disarmed. Set the button's active PWM to 1600-2000
(inclusive). Entering that range requests three beeps and `Person Tracking Ready`
once; holding it active does not repeat. Release it to a PWM outside that range
before pressing again. The first valid RC9 sample also triggers if already active
at service startup. Missing/invalid samples do not count as releases.
This button only sends notifications; it does not arm or start tracking.

The RC12 wheel is also a notification-only test in any flight mode, including
while disarmed. Turning it to the high end (PWM >=1800) sends `RC12 Wheel High`
and requests two beeps; turning to the low end (PWM <=1200) sends
`RC12 Wheel Low` and requests two beeps. Holding an endpoint does not repeat.
To trigger the same endpoint again, move below 1700 after high or above 1300
after low, then return to that endpoint. This 100-PWM release margin avoids
repeated notifications from jitter. Moving directly to the opposite endpoint
triggers its message immediately. The first valid sample also triggers if the
wheel is already at an endpoint when listening starts. Samples outside PWM
800-2200 or without channel 12 are ignored and do not count as releases.
Logs include `RC12 wheel transition: ...; PWM=...` plus the status-text and beep
send attempts. The two test messages are defined as `RC12_HIGH_MESSAGE` and
`RC12_LOW_MESSAGE` in `src/notifications.py`.

The service listens directly for `RC_CHANNELS` from the trusted flight controller
and requests it at 10 Hz through the existing MAVLink link. RC9 presses and RC12
endpoint events bypass the notification cooldown and honor the text/buzzer
enable switches. Logs show
`RC9 notification button activated: PWM=...` and `Person Tracking Ready` when a
press is detected. These logs confirm detection and the send attempt; verify
actual message reception on Herelink separately from hearing the aircraft buzzer.

Rule announcements begin when the flight state reaches `Running`. Only changes
to the rule that actually wins priority are announced. Repeated control cycles
stay quiet. During the configured cooldown, intermediate transitions are
coalesced to the current rule, which is announced once the interval elapses if
it still differs from the last announcement. The controller never sleeps to
space beeps; the flight controller plays the complete tune itself.

Configure notifications in the root `.env` and restart the service:

- `NOTIFICATIONS_TEXT_ENABLED` and `NOTIFICATIONS_BEEPS_ENABLED`: `true`/`false`,
  controlling each output independently.
- `NOTIFICATIONS_MIN_INTERVAL`: minimum seconds between rule/custom events
  (default example: 3). Zero disables the interval, while duplicate rules still
  remain silent. Service-ready, tracking-activation, RC9 button, and RC12 wheel
  announcements bypass the interval.
- `CAMERA_READY_TIMEOUT`: positive seconds to wait for the camera's first output
  (default example: 30). Timeout fails startup and closes the camera/link.
- `NOTIFICATIONS_BACKOFF_BEEPS`, `NOTIFICATIONS_FOLLOW_BEEPS`,
  `NOTIFICATIONS_SEARCH_BEEPS`, `NOTIFICATIONS_NONE_BEEPS`: integers from 0 to 6.
  Zero silences that rule's tune. Readiness always requests six beeps when sounds
  are enabled.

The helper also exposes `notify(message, beeps=0, severity=...)`,
`status_text(message, severity=...)`, and `beep(count)` for additional key events.
These custom calls share the interval; a custom event during cooldown is dropped.
All outgoing application status text is forced to `NOTICE`, including custom
events. The `severity` argument is accepted for compatibility but does not change
the transmitted severity. Incoming flight-controller messages keep their original
severity.
Status text must fit in one 50-byte UTF-8 message. Notification failures are
logged and do not stop movement control. Failed rule sends are deduplicated too.
SITL keeps notifications disabled by omitting the optional notifier from `Core`.

Notifications use MAVLink 2 framing, the learned aircraft system ID, and onboard
computer component ID 191, without changing DroneKit's normal command sender.
The aircraft must support ArduPilot's `PLAY_TUNE` and have a compatible enabled
buzzer. Herelink must receive forwarded `STATUSTEXT` packets; message visibility
depends on its ground-station app and connection. These are best-effort requests,
with no playback/display acknowledgement or replay for a controller that connects
later. Other aircraft alarms can interrupt a tune. See the
[MAVLink message definitions](https://mavlink.io/en/messages/common.html#PLAY_TUNE),
[ArduPilot tune implementation](https://github.com/ArduPilot/ardupilot/blob/master/libraries/AP_Notify/ToneAlarm.cpp),
and [ArduPilot routing guide](https://ardupilot.org/dev/docs/mavlink-routing-in-ardupilot.html).

## Packages

Pinned direct Python dependencies are in `requirements.txt` and
`simulation/requirements.txt`. Pip also installs their transitive dependencies.

| Purpose | Python packages |
| --- | --- |
| Flight-controller connection | `dronekit`, `pymavlink`, `pyserial`, `future` |
| OAK-D camera and video | `depthai`, `numpy`, `opencv-python` |
| Flight settings | `python-dotenv` |
| Optional simulation/tests | `dronekit-sitl`, `gpxpy`, `websockets`, `pytest` |

DepthAI is pinned to v2 because the camera uses `Pipeline`/`XLinkOut` v2 APIs.
NumPy/OpenCV versions are pinned together. OpenCV uses the CPU wheels; this
installer does not build CUDA-enabled OpenCV or replace JetPack/CUDA drivers.
Websockets is pinned to the legacy API used by the simulator. The small
`src/dronekit_compat.py` helper lets the released DroneKit import on Python 3.10+.
`depthai-sdk`, MAVProxy and the PyPI `asyncio` backport are not required by the
flight code; `asyncio` is part of Python's standard library.

System packages installed by the script:

- Python and build tools: `python3`, `python3-venv`, `python3-dev`,
  `build-essential`, `pkg-config`, `ca-certificates`.
- Native build libraries: `libxml2-dev`, `libxslt1-dev`, `zlib1g-dev`.
- USB and video libraries: `libusb-1.0-0`, `libudev1`, `libgl1`, `libglib2.0-0`
  (or `libglib2.0-0t64` on newer distributions), `libsm6`, `libxext6`, `libxrender1`.
- Service, device and account tools: `systemd`, `udev`, `usbutils`, `passwd`.

APT packages, account group membership, the generated unit, systemd's boot links,
and the rules under `/etc/udev/rules.d/` live outside the project. The installer
automatically adds `SERVICE_USER` to `dialout`, keeping existing memberships.
It installs `80-auto-drone-oak.rules` for Luxonis cameras and generates
`99-auto-drone-mavlink.rules` for the selected MAVLink tty, granting the `dialout`
group read/write access (`0660`). USB rules match vendor/product, serial number
and interface when available. Devices without a usable serial number match the
physical USB path instead. Onboard UART rules match the selected kernel port.
Both rules are reloaded and applied immediately, and the installer checks the
user's read/write access before installing/enabling or starting the service.
This permission check does not open the serial port or send MAVLink commands.

`.env`, `.venv/`, `.cache/pip/`, `logs/`, `videos/`, and the optional `killswitch`
file stay inside the project. New group membership works immediately for the
service and new installer checks. Log out and back in before running manual
camera/MAVLink tools from existing terminal sessions; no reboot is required.

Re-run the installer after moving the repository or changing `SERVICE_USER` or
`MAVLINK_PORT`, so the installed unit and device rules match the configuration.
If you change Python's major/minor version, move the old `.venv` aside first so
the installer can create a matching environment. A custom Python installation
must already support `venv`/`ensurepip` and provide matching development headers;
APT installs these components for the distribution's default Python.
Use `--start` when re-running the installer to also restart an active service.
View runtime logs with `journalctl -u Auto_Drone.service -f`.
