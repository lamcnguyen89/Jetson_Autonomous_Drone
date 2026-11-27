"""Simulation assets, resolved independently of the caller's working directory."""

import os
from pathlib import Path
import platform


SIMULATION_ROOT = Path(__file__).resolve().parent
ARDUPILOT_ROOT = SIMULATION_ROOT / "ardupilot"
DEFAULTS_FILE = ARDUPILOT_ROOT / "copter-hexa.parm"
SITL_BINARIES_BY_SYSTEM = {
    "Darwin": ("arducopter",),
    "Linux": ("arducopter-linux", "arducopter"),
}


def is_linux_binary(path):
    """Check an executable's ELF header to reject binaries for another platform."""
    with open(path, "rb") as binary:
        return binary.read(4) == b"\x7fELF"


def sitl_binary_path():
    """Resolve the correct bundled or overridden ArduPilot SITL executable."""
    override_path = os.environ.get("ARDUPILOT_SITL_BINARY")
    if override_path:
        return str(Path(override_path).expanduser().resolve())

    system = platform.system()
    binary_names = SITL_BINARIES_BY_SYSTEM.get(system)
    if binary_names is None:
        supported_systems = ", ".join(sorted(SITL_BINARIES_BY_SYSTEM))
        raise RuntimeError(
            f"No default ArduPilot SITL binary is configured for {system}. "
            f"Set ARDUPILOT_SITL_BINARY to your arducopter executable. "
            f"Configured systems: {supported_systems}."
        )

    for binary_name in binary_names:
        path = ARDUPILOT_ROOT / binary_name
        if path.is_file() and (system != "Linux" or is_linux_binary(path)):
            return str(path)

    expected_paths = ", ".join(str(ARDUPILOT_ROOT / name) for name in binary_names)
    raise FileNotFoundError(
        f"No usable ArduPilot SITL binary found for {system}. "
        f"Expected one of: {expected_paths}. "
        f"Add a build for your host's CPU architecture there, or set "
        f"ARDUPILOT_SITL_BINARY to the path of your executable."
    )
