# Jetson_Aurelia_Herelink_Autonomous_Drone
Code for Autonomous Drone using Jetson Orin Nano, the Herelink Video/RC System, and Luxonis Depth Camera

## Reading the code

The flight service separates sensor processing from flight decisions. Both real
camera backends publish the same meter-based person positions, so the controller
does not depend on a particular model or inference processor. Read the following
files in order for the main architecture; module docstrings and local comments
explain the less obvious choices at each step.

| Start here | Responsibility and connection to the next layer |
| --- | --- |
| [src/main.py](src/main.py) | Assembles the service, waits for readiness, and starts camera/control workers. |
| [src/config.py](src/config.py) | Validates flight JSON snapshots, polls while disarmed, and loads startup defaults and backend selection. |
| [src/camera/factory.py](src/camera/factory.py) | Chooses the camera adapter at startup. |
| [src/camera/runtime.py](src/camera/runtime.py) | Publishes batches and enforces capture-age freshness for both real backends. |
| [src/core.py](src/core.py) and [src/rules/base.py](src/rules/base.py) | Separate flight lifecycle from priority-ordered backoff/follow/search/hold decisions. |
| [src/commands.py](src/commands.py) | Converts meter errors and yaw rates into bounded velocities or geographic hold commands. |
| [src/filtered_vehicle.py](src/filtered_vehicle.py) and [src/notifications.py](src/notifications.py) | Handle autopilot heartbeat selection and operator feedback on the shared MAVLink link. |

For the inference details, follow either
[yolocam.py](src/camera/yolocam.py), where the OAK performs detection and spatial
measurement, or [jetson.py](src/camera/jetson.py), where synchronized OAK RGB/depth
feeds the Jetson pipeline. The latter delegates pixel/tensor/GPU work to
[tensorrt_yolo.py](src/camera/tensorrt_yolo.py), pixel-to-meter projection to
[spatial.py](src/camera/spatial.py), and person selection to `TargetLock`.

Installation is a separate lifecycle: [Install_Auto_Drone.sh](scripts/Install_Auto_Drone.sh)
installs dependencies and invokes [prepare_jetson_model.py](scripts/prepare_jetson_model.py)
to reuse or compile an engine before enabling the service. Model building is not
part of the flight worker. [simulation/environment.py](simulation/environment.py)
reuses Core with a virtual autopilot and synthetic detections; it tests movement
behavior independently of the real inference and depth pipelines.

## Installation and systemd startup

The installer targets a Jetson with Ubuntu/Debian, systemd, and JetPack's
TensorRT 10.x / CUDA 12 runtime (this Jetson uses Ubuntu 22.04).
It installs native packages with APT and Python packages into `.venv` in the
project root. `BASE_PYTHON` must match the installed JetPack TensorRT Python
bindings; Ubuntu 22.04's Python 3.10 works on this Jetson. The base flight
dependencies support Python 3.9-3.12.
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

This installs both Luxonis and Jetson inference dependencies and automatically
prepares the YOLO26m TensorRT engine at `JETSON_MODEL_PATH` (or its default).
No Jetson flag or separate model preparation command is required. A valid
existing engine is reused after checking its manifest, checksum, TensorRT
version, and GPU execution. Missing, damaged, or incompatible artifacts are
rebuilt. The initial preparation downloads pretrained weights and can take
several minutes; the measured first build on this Jetson took about 13 minutes.
The installer preserves your `.env` and its selected inference backend.

The script discovers the project location, so an absolute path to the installer
also works from another working directory. It fills in the project path and service
user in [systemd/Auto_Drone.service](systemd/Auto_Drone.service), validates dependencies
and the rendered service, then installs it as `/etc/systemd/system/Auto_Drone.service`.
Before enabling Auto_Drone, the installer stops, disables, and removes these
conflicting system services and their overrides if installed:
`dynamic_drone_target.service`, `compact-vla-clock.service`, `compact-vla-drone.service`,
`compact-vla-server.service`, `luxonis-rlds-recorder.service`,
`mavros-stream-monitor.service`, and `mavros.service`. Their application files
and data are preserved. Add `--start` to also start/restart Auto_Drone immediately.
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

## Manual Wi-Fi and time synchronization

Configure `WIFI_CONNECTIONS` in the root `.env` as a single-quoted JSON array
of saved NetworkManager profile names in priority order. List saved profiles
with `nmcli connection show`; profile names may differ from Wi-Fi SSIDs.
`WIFI_CONNECT_TIMEOUT` is a positive integer in seconds per connection attempt
(defaults to 90 when omitted).

```bash
sudo bash scripts/connect_internet.sh
sudo bash scripts/disable_internet.sh
```

The connect script reads the trusted `.env` directly, validates the Wi-Fi
settings, enables NTP and Wi-Fi, and tries each saved profile. Run it in a
terminal so NetworkManager can prompt for missing credentials. It needs system
`python3`, `nmcli`, `rfkill`, and `timedatectl`; no project virtual environment
is needed. The disable script needs no env settings: it disables NTP and blocks
all Wi-Fi radios, leaving Ethernet unchanged. It also requires `timeout`.
Disconnecting Wi-Fi ends SSH sessions using that connection.

These are manual helpers. `Auto_Drone.service` does not invoke them or manage
clock synchronization at boot. The copied `CLOCK_SYNC_MODE`,
`CLOCK_SYNC_TIMEOUT`, `CLOCK_PREP_WIFI_TIMEOUT`, and `CLOCK_PREP_SYNC_TIMEOUT`
settings were unused and have been removed. Enabling NTP requests time
synchronization; the connect script does not wait for synchronization to finish.
Run these helpers while the drone application is stopped to avoid changing host
time during a session.

## Flight configuration

Use [src/flight_parameters.json](src/flight_parameters.json) for flight tuning,
including changes made by a separate WebUI backend. Keep the project root `.env`
as the trusted defaults and machine configuration. All 13 tuning values are
documented in `.env.example`; there are no numeric defaults hidden in Python.
The serial baud rate is configured only through `.env` or the process environment.

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

Flight parameters are read from `src/flight_parameters.json` at startup, with
precedence **JSON override > process environment > .env**. The JSON path is also
absolute and independent of the working directory. This applies to the real
flight service and SITL. While the drone is disarmed, Core polls the JSON every
2 seconds and applies valid changes together as a new settings snapshot. The
file is not read or applied while armed; saved changes wait until a poll after
disarming. Invalid or unreadable updates are logged and retain the last valid
settings, with another attempt on the next poll. The supplied JSON lists every
editable parameter as `null`, preserving the existing defaults until you enter
a value. For example:

```json
{
  "ALTITUDE": 3.0,
  "SPEED": 0.5,
  "YAW_RATE": "",
  "defaults": {
    "ALTITUDE": 2.0,
    "SPEED": 1.0,
    "YAW_RATE": 25.0
  }
}
```

Use JSON numbers for overrides. `null`, an empty string (`""`), a whitespace-only
string, or an omitted key uses the existing environment/.env default for that
parameter. Saving `{}`, an empty file, or removing the file resets all overrides.
Zero is an actual value and must satisfy that parameter's validation rules.
Nonblank strings (including `"0.5"`), booleans, duplicate keys, malformed JSON,
and unsupported keys cause a configuration error. The entire object must be
valid; invalid overrides do not silently fall back to defaults.

At each flight-service startup, before opening the camera or connecting to the
controller, the service refreshes the `defaults` object with all 13 numeric
fallback values. These come from `.env` plus process-environment overrides, so
they match the values used when a tuning field is blank. Existing top-level
overrides are preserved. The example above abbreviates the generated defaults;
the actual file contains all 13. A missing or empty file is recreated with blank
override fields and the current defaults. Updating `.env` refreshes these display
values on the next service restart. Ordinary settings reads, imports, and
`--help` do not write the JSON file.

The WebUI can read `defaults.ALTITUDE`, for example, to show the altitude default,
and edit top-level `ALTITUDE` to set an override. Treat `defaults` as generated,
read-only display metadata: the flight code ignores its values, even if changed,
and replaces the entire object at the next service startup. Only allowed flight
defaults are exported; machine settings and other `.env` contents are excluded.
The displayed default is the fallback, not necessarily the running value when
an override is set. Saved override changes take effect on the next disarmed poll.

Only the 13 tuning keys in the table above are editable through JSON; `BAUD_RATE`
is excluded. Machine settings, inference selection, and notifications cannot be
overridden through this file. `YAW_MAX_DAMP` remains accepted for compatibility
but is currently unused by the movement rules.

The WebUI implementation lives separately. Its backend should use a fixed path
to this JSON file, validate the merged settings before saving, and never expose
a general file-writing endpoint or write `.env`. Filesystem isolation requires
a separate WebUI account with write access limited to the JSON file; do not
grant it write access to `.env`, the repository root, or the `src` directory.
If using atomic replacement, use a trusted writer with narrowly scoped authority
to replace only this file rather than giving the WebUI write access to `src`.
This repository change does not configure the separate WebUI's account or permissions.
The flight-service account needs write access to `src` to publish the JSON file
atomically. Existing file ownership and access metadata are preserved. Serialize
WebUI saves with service restarts so two writers do not overwrite one another's
updates. A defaults-publication error prevents hardware startup and is reported
as a configuration error.

Settings are converted to finite numbers and checked at startup. Missing,
nonnumeric or negative values cause an error. `BAUD_RATE` must be a positive
integer. Altitude, following distance and
heartbeat timeout must be positive; altitude tolerance must be below the target
altitude, the backoff trigger must not exceed the following distance, and
detection confidence must be between 0 and 1.

Each settings snapshot is immutable; validated JSON changes replace the shared
snapshot while disarmed. All 13 editable keys support this, including camera
confidence thresholds. Blank or removed overrides use the `.env` plus process
environment defaults captured at startup. Polling only reads the JSON; it does
not rewrite the file or refresh its generated `defaults` metadata.

After changing `.env`, machine settings, inference selection, or notifications,
restart the application, SITL backend, or service while disarmed:

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

## Select inference processing: OAK or Jetson

Both backends use the OAK RGB camera and stereo depth sensors. Select the
processor in the project `.env` and restart the service while the vehicle is
disarmed:

```dotenv
INFERENCE_BACKEND=luxonis
LUXONIS_MODEL_PATH=models/yolo-v4-tiny-tf_openvino_2021.4_6shave.blob
JETSON_MODEL_PATH=models/yolo26m-fp16.engine
PERCEPTION_MAX_AGE_SECONDS=0.3
TARGET_LOST_TIMEOUT_SECONDS=2.0
```

Set `INFERENCE_BACKEND=jetson` for **YOLO26m with TensorRT FP16 on GPU 0**.
Set it to `luxonis` to retain TinyYOLO v4 inference on the OAK VPU. Existing
`.env` files without these options default to Luxonis. Paths are relative to
the project root, regardless of the process working directory. Process
environment values override `.env`; restart after changes. Invalid backends
fail configuration validation; there is no automatic CPU or OAK fallback when
Jetson mode fails.

### Prepare YOLO26m on the Jetson

The GPU backend targets JetPack's TensorRT 10.x and CUDA 12 runtime. On the
development Orin NX this is TensorRT 10.7 / CUDA 12.6 with Python 3.10. Keep the
JetPack runtime; do not replace it with an unrelated PyPI TensorRT version.
The normal installer installs both backends and prepares the model automatically:

```bash
sudo bash scripts/Install_Auto_Drone.sh
```

The former `--with-jetson` flag is still accepted for compatibility and is no
longer necessary. The installer invokes `scripts/prepare_jetson_model.py
--if-needed` as `SERVICE_USER` before installing/enabling or restarting the
service. Preparation failure aborts the installation before those service
changes. The script can still be run directly for maintenance:

```bash
# Check and reuse the engine, or prepare it if needed:
.venv/bin/python scripts/prepare_jetson_model.py --if-needed

# Force a fresh export and build:
.venv/bin/python scripts/prepare_jetson_model.py
```

The preparation script never opens the camera or MAVLink port or changes the
service. It downloads official `yolo26m.pt` weights to `models/`, exports a static
640x640, batch-one, end-to-end ONNX model, then builds an FP16 TensorRT engine on
this Jetson. Initial preparation requires internet access and can take several
minutes. The script validates the model and runs GPU warmup before publishing
the engine and its checksum manifest. Generated artifacts are ignored by Git.
Rebuild after changing the Jetson hardware or TensorRT runtime. Exported
YOLO26m ONNX can also be supplied with `--onnx models/yolo26m.onnx` to skip the
PyTorch export. The default engine is `models/yolo26m-fp16.engine`; startup
requires its companion `.json` manifest.

PyTorch is used on CPU for model export and tracker utilities. Flight-time
neural network inference uses TensorRT and CUDA directly, so a CUDA-enabled
PyTorch build and CUDA-enabled OpenCV are unnecessary. Only the installed
JetPack TensorRT package is loaded from the system when absent from the venv;
other system Python packages are not added to the isolated environment.

Test actual GPU inference and BoT-SORT without claiming the OAK or connecting
to the flight controller:

```bash
.venv/bin/python utils/check_jetson_inference.py --image /path/to/person.jpg
```

This reports detector-plus-tracker timing on repeated copies of the image;
it does not measure camera transfer, depth calculation, or flight control.

Validation on the development Orin NX (November 20, 2026): the FP16 engine
detected the same four people as the PyTorch reference on Ultralytics' `bus.jpg`,
with matched box IoUs above 0.998. On 100 repeats of a 640x360 version of the
image, preprocessing/TensorRT/decoding averaged 32.4 ms and BoT-SORT averaged
10.5 ms (42.9 ms combined mean, 50.7 ms combined p95). These are image benchmark
measurements, not live-camera throughput guarantees. The flight service stayed
on Luxonis throughout validation, so live RGB/depth alignment and capture-to-
result latency remain to be checked using the camera-only utility below.

After the engine is prepared, change `.env` to `INFERENCE_BACKEND=jetson` and,
while disarmed, restart with `sudo systemctl restart Auto_Drone.service`.
Switch back with `INFERENCE_BACKEND=luxonis` and the same restart. Preparation
alone does not switch the running service.

### Tracking and depth contract

Jetson mode runs the OAK's RGB acquisition and stereo depth pipeline without
an OAK neural network node. DepthAI Sync groups pair 640x360 RGB ISP images
with RGB-aligned depth within 15 ms; the host output queue holds at most one
group. RGB is letterboxed to 640x640 for YOLO26m and decoded boxes are mapped
back into the original image. A calibrated projection of the median valid
depth in the central half of each box produces `Detection(x, y, z, confidence,
fps, track_id)`. Coordinates retain the existing convention: meters, X right,
Y up, Z forward. Depth outside 250–10000 mm, inadequate valid depth, and people
at or below 0.5 m are rejected.

BoT-SORT uses sparse optical flow camera-motion compensation. Its explicit
configuration lives in `src/camera/botsort.yaml`; appearance re-identification
is disabled, so IDs are associations rather than guaranteed person identity.
Low-confidence detections assist association, but flight-facing results must
pass `DETECTION_THRESH`. New tracks require at least the higher of that setting
and BoT-SORT's high-confidence threshold (0.25 by default).

The nearest valid person is selected initially. `closestDetection()` then
returns that track while other people pass closer. If its detection or valid
depth disappears, tracking movement holds for `TARGET_LOST_TIMEOUT_SECONDS`.
After that interval the lock is released and another nearest valid person may
be selected. With a healthy pipeline and no target, the existing search rule
operates. This policy allows target changes after the timeout; it is not a
permanent identity lock or a person re-identification system.

Both real backends enforce capture-age freshness, including USB transfer and
inference time. Empty current batches count as healthy output. Failed, stopped,
or stale perception suppresses follow/backoff and search movement in favor of
the existing guided hold command. Startup readiness requires the first current
batch. Logs identify the backend and device; Jetson mode additionally reports
capture-to-result latency, processing FPS, and selected track ID every five
seconds. A persistent stale feed remains in hold until current output resumes.

For a live camera-only preview, stop the flight service while disarmed to
release the OAK, then run `.venv/bin/python utils/camera.py` (or add
`--headless --seconds 30`). It uses the selected backend and never connects to
MAVLink. The camera cannot be shared with the running flight service. Verify
RGB/depth alignment and latency on this path before flight; use USB 3 where
available. The 0.3-second age budget is an initial limit to validate on the
actual pipeline, not a measured latency guarantee.

References: [Ultralytics Jetson deployment](https://docs.ultralytics.com/guides/nvidia-jetson),
[YOLO26 models](https://docs.ultralytics.com/models/yolo26),
[BoT-SORT integration](https://docs.ultralytics.com/modes/track), and
[TensorRT engine compatibility](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/engine-compatibility.html).

## Packages

Pinned direct Python dependencies are in `requirements.txt`,
`requirements-jetson.txt`, and `simulation/requirements.txt`. The installer
includes Jetson dependencies by default. Pip also installs transitive dependencies.

| Purpose | Python packages |
| --- | --- |
| Flight-controller connection | `dronekit`, `pymavlink`, `pyserial`, `future` |
| OAK-D camera and video | `depthai`, `numpy`, `opencv-python` |
| YOLO26m export and BoT-SORT | `ultralytics`, `torch`, `torchvision`, `scipy`, `lap`, `onnx` |
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
- JetPack TensorRT tools and Python bindings: `libnvinfer-bin`, `python3-libnvinfer`.

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
