"""Typed, immutable settings read once from the project-root .env at startup.

Flight tuning, notifications and inference selection have separate settings
objects so each component receives the values relevant to its responsibility.
Environment variables override file values, matching systemd deployments.
Runtime code reads these snapshots; editing .env takes effect after restart.
"""

from dataclasses import dataclass, fields
import math
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


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


def load_settings(
    env_file: Path = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> FlightSettings:
    """Read required flight values without modifying or expanding the environment."""
    values = _read_values(env_file, environ)
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


# Immutable for the lifetime of this process; restart after editing flight values.
settings = load_settings()
notification_settings = load_notification_settings()
inference_settings = load_inference_settings()
