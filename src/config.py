"""Typed settings with validated flight snapshots refreshed while disarmed.

Flight tuning, notifications and inference selection have separate settings
objects so each component receives the values relevant to its responsibility.
Environment variables override .env values, matching systemd deployments.
Flight-only JSON overrides take precedence; blank values retain env defaults.
Runtime code reads the current flight snapshot. Environment defaults,
notifications and inference selection remain fixed until restart.
"""

from dataclasses import dataclass, fields
import json
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Mapping, Optional

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
FLIGHT_PARAMETERS_FILE = Path(__file__).resolve().parent / "flight_parameters.json"
FLIGHT_PARAMETER_POLL_INTERVAL = 2.0
logger = logging.getLogger(__name__)

# Keep machine settings, notifications, and inference selection outside the
# WebUI-editable contract. New FlightSettings fields are not exposed implicitly.
EDITABLE_FLIGHT_KEYS = frozenset({
    "ALTITUDE", "ALTITUDE_FUZZINESS", "MINIMUM_DISTANCE", "BACKOFF_DISTANCE",
    "HEARTBEAT_TIMEOUT", "YAW_RATE", "YAW_MAX_DAMP", "SPEED",
    "FOLLOW_VELOCITY_GAIN", "FOLLOW_POSITION_DEADBAND",
    "ALTITUDE_VELOCITY_GAIN", "MAX_VERTICAL_SPEED", "DETECTION_THRESH",
})


class ConfigurationError(ValueError):
    """A required flight setting is absent or invalid."""


@dataclass(frozen=True)
class FlightSettings:
    """Control gains, distance limits and shared detection-confidence threshold."""
    baud_rate: int
    altitude: float
    altitude_fuzziness: float
    minimum_distance: float
    backoff_distance: float
    heartbeat_timeout: float
    yaw_rate: float
    yaw_max_damp: float
    speed: float
    follow_velocity_gain: float
    follow_position_deadband: float
    altitude_velocity_gain: float
    max_vertical_speed: float
    detection_thresh: float

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"{field.name.upper()} must be a finite, nonnegative number")
        for name in ("baud_rate", "altitude", "minimum_distance", "heartbeat_timeout"):
            if getattr(self, name) == 0:
                raise ConfigurationError(f"{name.upper()} must be greater than zero")
        if self.altitude_fuzziness >= self.altitude:
            raise ConfigurationError("ALTITUDE_FUZZINESS must be less than ALTITUDE")
        if self.backoff_distance > self.minimum_distance:
            raise ConfigurationError("BACKOFF_DISTANCE must not exceed MINIMUM_DISTANCE")
        if self.detection_thresh > 1:
            raise ConfigurationError("DETECTION_THRESH must be between 0 and 1")


def _read_values(env_file, environ):
    # Use a project-root file rather than the caller's working directory. Disable
    # interpolation so Python reads the same literal values as the service file.
    env_file = Path(env_file)
    if not env_file.is_file():
        raise ConfigurationError(f"Missing {env_file}; copy the project .env.example to .env")
    values = dict(dotenv_values(env_file, interpolate=False))
    values.update(os.environ if environ is None else environ)
    return values


def _unique_json_object(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ConfigurationError(f"Duplicate flight setting: {key}")
        values[key] = value
    return values


def _read_flight_document(path):
    """Read overrides and generated display metadata without writing the file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f"Cannot read {path}: {error}") from error

    if not text.strip():
        return {}
    try:
        overrides = json.loads(text, object_pairs_hook=_unique_json_object)
    except ValueError as error:
        raise ConfigurationError(f"Invalid flight overrides in {path}: {error}") from error
    if not isinstance(overrides, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    unknown = set(overrides) - EDITABLE_FLIGHT_KEYS - {'defaults'}
    if unknown:
        raise ConfigurationError(f"Unsupported flight settings in {path}: {', '.join(sorted(unknown))}")
    if 'defaults' in overrides and not isinstance(overrides['defaults'], dict):
        raise ConfigurationError(f"defaults in {path} must be a JSON object")
    return overrides


def _flight_override_values(document, path):
    """Ignore display defaults; only top-level tuning keys have authority."""
    values = {}
    for key, value in document.items():
        if key == 'defaults':
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        # bool is an int subclass, but true/false are not flight tuning values.
        if type(value) not in (int, float):
            raise ConfigurationError(f"{key} in {path} must be a JSON number, null or blank string")
        values[key] = str(value)
    return values


def load_settings(
    env_file: Path = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
    overrides_file: Optional[Path] = None,
) -> FlightSettings:
    """Read JSON > process environment > .env, then validate the merged values.

    By default, read src/flight_parameters.json regardless of the working
    directory. An explicit overrides_file allows validation of another file.
    """
    values = _read_values(env_file, environ)
    path = FLIGHT_PARAMETERS_FILE if overrides_file is None else overrides_file
    values.update(_flight_override_values(_read_flight_document(path), path))
    return _parse_flight_settings(values, env_file)


def _parse_flight_settings(values, env_file):
    parsed = {}
    for field in fields(FlightSettings):
        key = field.name.upper()
        value = values.get(key)
        if value is None or not value.strip():
            raise ConfigurationError(f"Set {key} in {env_file}")
        try:
            parsed[field.name] = int(value) if field.type is int else float(value)
        except ValueError as error:
            expected = "an integer" if field.type is int else "a number"
            raise ConfigurationError(f"{key} must be {expected}, got {value!r}") from error
    return FlightSettings(**parsed)


def load_flight_overrides(defaults: FlightSettings, overrides_file=None) -> FlightSettings:
    """Validate JSON against startup defaults without rereading machine settings."""
    path = FLIGHT_PARAMETERS_FILE if overrides_file is None else overrides_file
    values = {field.name.upper(): str(getattr(defaults, field.name))
              for field in fields(FlightSettings)}
    values.update(_flight_override_values(_read_flight_document(path), path))
    return _parse_flight_settings(values, ENV_FILE)


class FlightParameterPoller:
    """Called by Core; read JSON at most every two seconds, only while disarmed."""

    def __init__(self, clock=None):
        self._clock = time.monotonic if clock is None else clock
        self._next_poll = self._clock() + FLIGHT_PARAMETER_POLL_INTERVAL

    def poll(self, vehicle):
        global settings
        now = self._clock()
        if now < self._next_poll:
            return False
        self._next_poll = now + FLIGHT_PARAMETER_POLL_INTERVAL
        if vehicle.armed is not False:
            return False
        try:
            candidate = load_flight_overrides(_flight_defaults)
        except ConfigurationError as error:
            logger.warning('Ignoring invalid flight parameter update; keeping last valid settings: %s', error)
            return False
        # Arming can arrive on DroneKit's I/O thread while the file is read.
        if vehicle.armed is not False or candidate == settings:
            return False
        previous = settings
        # Publish the entire immutable snapshot in one assignment. Consumers
        # read config.settings rather than retaining the startup object.
        settings = candidate
        changed = {field.name.upper(): getattr(candidate, field.name)
                   for field in fields(FlightSettings)
                   if getattr(candidate, field.name) != getattr(previous, field.name)}
        logger.info('Reloaded flight parameters while disarmed: %s', changed)
        return True


def refresh_flight_parameter_defaults(
    env_file: Path = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
    overrides_file: Optional[Path] = None,
):
    """Publish actual fallback values at service startup, preserving overrides.

    Only editable numeric flight defaults are exported; .env is never written.
    Loading settings or importing config does not publish or change any files.
    """
    path = Path(FLIGHT_PARAMETERS_FILE if overrides_file is None else overrides_file)
    values = _read_values(env_file, environ)
    defaults = _parse_flight_settings(values, env_file)
    document = _read_flight_document(path)
    # Validate overrides before publishing a document the service cannot use.
    _parse_flight_settings({**values, **_flight_override_values(document, path)}, env_file)
    published_defaults = {}
    for field in fields(FlightSettings):
        key = field.name.upper()
        if key in EDITABLE_FLIGHT_KEYS:
            document.setdefault(key, None)
            published_defaults[key] = getattr(defaults, field.name)
    document['defaults'] = published_defaults

    temporary_path = None
    try:
        previous = path.stat() if path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix=f'.{path.name}.', suffix='.tmp', delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(document, output, indent=2, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        if previous is not None:
            # Preserve ownership, mode and Linux ACLs for the WebUI's file access.
            current = temporary_path.stat()
            if (current.st_uid, current.st_gid) != (previous.st_uid, previous.st_gid):
                os.chown(temporary_path, previous.st_uid, previous.st_gid)
            shutil.copystat(path, temporary_path)
            os.utime(temporary_path, None)
        else:
            temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
    except OSError as error:
        raise ConfigurationError(f"Cannot publish flight defaults to {path}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class NotificationSettings:
    """Operator feedback settings, separate from movement authority and tuning."""
    text_enabled: bool
    beeps_enabled: bool
    min_interval: float
    camera_ready_timeout: float
    backoff_beeps: int
    follow_beeps: int
    search_beeps: int
    none_beeps: int

    def __post_init__(self):
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ConfigurationError("NOTIFICATIONS_MIN_INTERVAL must be finite and nonnegative")
        if not math.isfinite(self.camera_ready_timeout) or self.camera_ready_timeout <= 0:
            raise ConfigurationError("CAMERA_READY_TIMEOUT must be finite and greater than zero")
        for name in ('backoff_beeps', 'follow_beeps', 'search_beeps', 'none_beeps'):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 6:
                raise ConfigurationError(f"NOTIFICATIONS_{name.upper()} must be an integer from 0 to 6")


def load_notification_settings(
    env_file: Path = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> NotificationSettings:
    values = _read_values(env_file, environ)
    parsed = {}
    for field in fields(NotificationSettings):
        key = 'CAMERA_READY_TIMEOUT' if field.name == 'camera_ready_timeout' else 'NOTIFICATIONS_' + field.name.upper()
        value = values.get(key)
        if value is None or not value.strip():
            raise ConfigurationError(f"Set {key} in {env_file}")
        try:
            if field.type is bool:
                if value.strip().lower() not in ('true', 'false'):
                    raise ValueError('expected true or false')
                parsed[field.name] = value.strip().lower() == 'true'
            else:
                parsed[field.name] = int(value) if field.type is int else float(value)
        except ValueError as error:
            raise ConfigurationError(f"Invalid {key}: {value!r}") from error
    return NotificationSettings(**parsed)


@dataclass(frozen=True)
class InferenceSettings:
    """Backend/artifact selection and independent perception/target-loss time limits.

    max_age governs whether a measurement is current enough for movement.
    target_lost_timeout governs how long to reserve an absent person's track ID.
    Increasing the latter never makes old positions eligible for movement.
    """
    backend: str = 'luxonis'
    luxonis_model_path: Path = ENV_FILE.parent / 'models/yolo-v4-tiny-tf_openvino_2021.4_6shave.blob'
    jetson_model_path: Path = ENV_FILE.parent / 'models/yolo26m-fp16.engine'
    max_age: float = 0.3
    target_lost_timeout: float = 2.0

    def __post_init__(self):
        if self.backend not in ('luxonis', 'jetson'):
            raise ConfigurationError('INFERENCE_BACKEND must be luxonis or jetson')
        for name in ('max_age', 'target_lost_timeout'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                key = 'PERCEPTION_MAX_AGE_SECONDS' if name == 'max_age' else 'TARGET_LOST_TIMEOUT_SECONDS'
                raise ConfigurationError(f'{key} must be finite and greater than zero')


def load_inference_settings(env_file: Path = ENV_FILE, environ=None) -> InferenceSettings:
    # Defaults preserve existing installations whose .env predates backend selection.
    values = _read_values(env_file, environ)
    defaults = InferenceSettings()
    parsed = {}
    for field, key in (
        ('backend', 'INFERENCE_BACKEND'),
        ('luxonis_model_path', 'LUXONIS_MODEL_PATH'),
        ('jetson_model_path', 'JETSON_MODEL_PATH'),
        ('max_age', 'PERCEPTION_MAX_AGE_SECONDS'),
        ('target_lost_timeout', 'TARGET_LOST_TIMEOUT_SECONDS'),
    ):
        value = values.get(key, getattr(defaults, field))
        if value is None or not str(value).strip():
            raise ConfigurationError(f'Set {key} in {env_file}')
        if field.endswith('_path'):
            # Both systemd and manual tools refer to the same model artifacts,
            # even when invoked from different working directories.
            value = Path(value)
            if not value.is_absolute():
                value = ENV_FILE.parent / value
        elif field in ('max_age', 'target_lost_timeout'):
            try:
                value = float(value)
            except (ValueError, TypeError) as error:
                raise ConfigurationError(f'{key} must be a number') from error
        else:
            value = str(value).strip().lower()
        parsed[field] = value
    return InferenceSettings(**parsed)


# Keep fallback values fixed for the process lifetime. Only JSON tuning is live.
_flight_defaults = _parse_flight_settings(_read_values(ENV_FILE, None), ENV_FILE)
settings = load_flight_overrides(_flight_defaults)
notification_settings = load_notification_settings()
inference_settings = load_inference_settings()
