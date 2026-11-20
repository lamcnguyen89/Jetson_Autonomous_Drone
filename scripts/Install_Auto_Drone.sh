#!/usr/bin/env bash
set -euo pipefail

# Installation order: resolve account/config -> native/Python dependencies ->
# validate or prepare YOLO26m -> install device permissions -> remove conflicting
# services -> enable service.
# Model preparation runs as SERVICE_USER and completes before service changes;
# only --start requests an immediate restart. Inference selection stays in .env.

usage() {
    echo "Usage: sudo $0 [--with-sitl] [--start]"
    echo "Install both inference backends, project .venv, permissions and the service."
    echo "Prepare YOLO26m automatically when no valid TensorRT engine is available."
    echo "Creates a missing .env from .env.example; keeps existing configuration."
    echo "Uninstalls conflicting Compact VLA, RLDS recorder and MAVROS services."
    echo "Connect the MAVLink controller and check the configured port before running."
    echo "  --with-sitl  Also install simulation and test dependencies."
    echo "  --start      Also start/restart the service now (always enabled at boot)."
}

start_service=false
with_sitl=false
while (( $# )); do
    case "$1" in
        --start) start_service=true ;;
        --with-sitl) with_sitl=true ;;
        --with-jetson) : ;; # Accepted for compatibility; Jetson setup is now the default.
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
    shift
done

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="$project_root/.env"
unit_name=Auto_Drone.service
venv_dir="$project_root/.venv"
venv_python="$venv_dir/bin/python"

if (( EUID != 0 )); then
    echo "Run this installer with sudo to install system packages and the unit." >&2
    exit 1
fi
if [[ ! -f /etc/debian_version ]] || ! command -v apt-get >/dev/null; then
    echo "This installer requires an Ubuntu/Debian system with apt-get and systemd." >&2
    exit 1
fi

if [[ ! -f "$env_file" ]]; then
    if [[ -e "$env_file" || -L "$env_file" ]]; then
        echo "$env_file exists but is not a regular file; refusing to replace it." >&2
        exit 1
    fi
    env_example="$project_root/.env.example"
    if [[ ! -f "$env_example" ]]; then
        echo "Cannot create $env_file: template $env_example is missing." >&2
        exit 1
    fi
    # sudo identifies the invoking account. For a direct root invocation, use
    # the checkout owner so the generated configuration remains user-editable.
    initial_user="${SUDO_USER:-$(stat -c %U -- "$project_root")}"
    if ! id "$initial_user" >/dev/null 2>&1; then
        echo "Cannot determine the configuration owner: $initial_user" >&2
        exit 1
    fi
    new_env_file="$(mktemp "$project_root/.env.XXXXXX")"
    trap 'rm -f -- "$new_env_file"' EXIT
    awk -v service_user="$initial_user" '
        $0 == "SERVICE_USER=your-linux-username" {
            print "SERVICE_USER=" service_user
            next
        }
        { print }
    ' "$env_example" > "$new_env_file"
    chown "$initial_user:$(id -gn "$initial_user")" "$new_env_file"
    chmod 0644 "$new_env_file"
    # Publish atomically without overwriting a file created in the meantime.
    if ! ln -T -- "$new_env_file" "$env_file"; then
        echo "Could not create $env_file; an existing file will not be overwritten." >&2
        exit 1
    fi
    rm -f -- "$new_env_file"
    trap - EXIT
    echo "Created $env_file from .env.example for $initial_user."
fi

# This is trusted project configuration, shared with systemd. Use literal values
# so Bash and systemd interpret the settings consistently.
unset SERVICE_USER BASE_PYTHON MAVLINK_PORT
source "$env_file"
: "${SERVICE_USER:?Set SERVICE_USER in .env}"
: "${MAVLINK_PORT:?Set MAVLINK_PORT in .env}"
BASE_PYTHON="${BASE_PYTHON:-/usr/bin/python3}"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service user does not exist: $SERVICE_USER" >&2
    exit 1
fi
if [[ "$BASE_PYTHON" != /* ]]; then
    echo "BASE_PYTHON must be an absolute interpreter path." >&2
    exit 1
fi
if [[ "$MAVLINK_PORT" != /dev/* ]]; then
    echo "MAVLINK_PORT must be a device path under /dev/." >&2
    exit 1
fi
if [[ ! -c "$MAVLINK_PORT" ]]; then
    echo "Connect the controller and set MAVLINK_PORT to its serial device in .env: $MAVLINK_PORT" >&2
    exit 1
fi

cd -- "$project_root"
run_as_service_user() {
    # Keep model downloads, Python packages and caches writable by the account
    # that runs inference. Root is only needed for system-level installation.
    runuser -u "$SERVICE_USER" -- env \
        PIP_CACHE_DIR="$project_root/.cache/pip" \
        PIP_DISABLE_PIP_VERSION_CHECK=1 "$@"
}
if ! run_as_service_user test -w "$project_root" || \
        ! run_as_service_user test -r "$env_file"; then
    echo "$SERVICE_USER needs write access to the project and read access to .env." >&2
    exit 1
fi

# Python stays isolated in .venv. APT provides native libraries and build tools
# for MAVLink/lxml wheels that may need compiling on the target architecture.
# TensorRT comes from JetPack's APT repository so its native libraries and Python
# bindings match the platform. Pip supplies model/tracker code inside the venv.
system_packages=(
    ca-certificates python3 python3-venv python3-dev
    build-essential pkg-config libxml2-dev libxslt1-dev zlib1g-dev
    libusb-1.0-0 libudev1 libgl1 libsm6 libxext6 libxrender1
    systemd udev usbutils passwd
    libnvinfer-bin python3-libnvinfer
)
apt-get update
if apt-cache show libglib2.0-0t64 >/dev/null 2>&1; then
    system_packages+=(libglib2.0-0t64)
else
    system_packages+=(libglib2.0-0)
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${system_packages[@]}"

if ! getent group dialout >/dev/null; then
    groupadd --system dialout
fi
added_dialout=false
case " $(id -nG "$SERVICE_USER") " in
    *" dialout "*) ;;
    *)
        usermod -aG dialout "$SERVICE_USER"
        added_dialout=true
        ;;
esac

if [[ ! -x "$BASE_PYTHON" ]]; then
    echo "BASE_PYTHON is not executable: $BASE_PYTHON" >&2
    exit 1
fi
base_version="$("$BASE_PYTHON" - <<'PY'
import sys
if not (3, 9) <= sys.version_info[:2] <= (3, 12):
    raise SystemExit('The pinned dependencies require Python 3.9-3.12. Set BASE_PYTHON in .env.')
print(f'{sys.version_info.major}.{sys.version_info.minor}')
PY
)"

service_group="$(id -gn "$SERVICE_USER")"
install -d -o "$SERVICE_USER" -g "$service_group" \
    "$project_root/.cache" "$project_root/.cache/pip" \
    "$project_root/logs" "$project_root/videos"
if [[ ! -e "$venv_dir" ]]; then
    run_as_service_user "$BASE_PYTHON" -m venv "$venv_dir"
elif [[ ! -f "$venv_dir/pyvenv.cfg" || ! -x "$venv_python" ]]; then
    echo "$venv_dir exists but is not a usable virtual environment. Move it aside and retry." >&2
    exit 1
fi
run_as_service_user "$venv_python" - "$venv_dir" "$base_version" <<'PY'
from pathlib import Path
import sys

venv_dir = Path(sys.argv[1])
expected_version = tuple(map(int, sys.argv[2].split('.')))
config = (venv_dir / 'pyvenv.cfg').read_text().lower()
if (Path(sys.prefix).resolve() != venv_dir.resolve()
        or sys.prefix == sys.base_prefix
        or 'include-system-site-packages = false' not in config
        or sys.version_info[:2] != expected_version):
    raise SystemExit('Existing .venv does not match BASE_PYTHON or is not isolated. Move it aside and retry.')
PY

# Run pip as the service user and only inside .venv, not the system interpreter.
run_as_service_user "$venv_python" -m pip install --upgrade \
    'pip<26' 'setuptools<81' 'wheel<0.46'
# Require published camera/array/OpenCV wheels instead of starting large native
# builds on a Jetson. The pinned versions have ARM64 wheels for Python 3.9-3.12.
run_as_service_user "$venv_python" -m pip install \
    --only-binary=depthai,numpy,opencv-python -r "$project_root/requirements-jetson.txt"
if [[ "$with_sitl" == true ]]; then
    run_as_service_user "$venv_python" -m pip install \
        --only-binary=depthai,numpy,opencv-python -r "$project_root/simulation/requirements.txt"
fi
run_as_service_user env PYTHONPATH="$project_root/src" YOLO_AUTOINSTALL=false \
    "$venv_python" - <<'PY'
from camera.tensorrt_yolo import load_tensorrt
from camera.jetson import create_tracker
print('JetPack TensorRT:', load_tensorrt().__version__)
create_tracker()
PY
run_as_service_user "$venv_python" -m pip check
# Reuse a validated engine; otherwise download/export/build as the service user.
# Complete preparation before installing/enabling/restarting the flight service.
# --if-needed checks checksums, TensorRT version and actual GPU execution, so
# merely finding a .engine filename never skips a required preparation step.
run_as_service_user "$venv_python" "$project_root/scripts/prepare_jetson_model.py" --if-needed
# --help imports the flight application without opening hardware or flying.
run_as_service_user "$venv_python" "$project_root/src/main.py" --help >/dev/null
if [[ "$with_sitl" == true ]]; then
    run_as_service_user "$venv_python" - <<'PY'
from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()
import dronekit_sitl
import gpxpy
import pytest
import websockets
PY
fi

# Keep the rendered temporary unit in the repository. In addition to APT packages,
# only installed units/rules, account group membership and boot links go outside it.
render_dir="$(mktemp -d "$project_root/scripts/.install.XXXXXX")"
trap 'rm -rf -- "$render_dir"' EXIT
"$BASE_PYTHON" - "$project_root" "$SERVICE_USER" "$render_dir/$unit_name" <<'PY'
from pathlib import Path
import sys

project_root, service_user, output = sys.argv[1:]

def systemd_escape(value):
    # These directives accept literal paths, including interior spaces.
    if '\n' in value or '\r' in value:
        raise ValueError('The project path and service user must not contain newlines')
    return value.replace('%', '%%')

template = (Path(project_root) / 'scripts' / 'Auto_Drone.service').read_text()
rendered = template.replace('@PROJECT_ROOT@', systemd_escape(project_root))
rendered = rendered.replace('@SERVICE_USER@', systemd_escape(service_user))
Path(output).write_text(rendered)
PY

systemd-analyze verify "$render_dir/$unit_name"
# Inspect the connected tty without opening it or sending MAVLink traffic.
udevadm settle --timeout=30
"$BASE_PYTHON" "$project_root/scripts/mavlink_udev.py" \
    "$MAVLINK_PORT" "$render_dir/99-auto-drone-mavlink.rules"
mavlink_device="$(readlink -f -- "$MAVLINK_PORT")"
install -m 0644 "$project_root/scripts/80-auto-drone-oak.rules" \
    /etc/udev/rules.d/80-auto-drone-oak.rules
install -m 0644 "$render_dir/99-auto-drone-mavlink.rules" \
    /etc/udev/rules.d/99-auto-drone-mavlink.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=03e7
udevadm trigger --action=change "$mavlink_device"
udevadm settle --timeout=30
# A fresh runuser process picks up new groups immediately. Check permissions
# without opening the serial port (which could reset or claim the controller).
if ! run_as_service_user test -r "$MAVLINK_PORT" || \
        ! run_as_service_user test -w "$MAVLINK_PORT"; then
    echo "$SERVICE_USER still cannot read/write $MAVLINK_PORT after applying the udev rule." >&2
    stat -Lc '%A %U:%G %n' "$MAVLINK_PORT" >&2
    echo "Check for conflicting udev rules, then re-run the installer." >&2
    exit 1
fi
echo "Verified $SERVICE_USER can read/write $MAVLINK_PORT."

# Stop competing hardware users before enabling Auto_Drone. Wait until all
# validation succeeds so a failed preparation leaves existing services intact.
conflicting_units=(
    compact-vla-clock.service
    compact-vla-drone.service
    compact-vla-server.service
    luxonis-rlds-recorder.service
    mavros-stream-monitor.service
    mavros.service
)
system_unit_dirs=(
    /etc/systemd/system /run/systemd/system
    /usr/local/lib/systemd/system /usr/lib/systemd/system /lib/systemd/system
)
for conflicting_unit in "${conflicting_units[@]}"; do
    load_state="$(systemctl show --property=LoadState --value "$conflicting_unit")"
    if [[ "$load_state" != not-found ]]; then
        echo "Stopping and disabling $conflicting_unit."
        systemctl stop "$conflicting_unit"
        systemctl disable "$conflicting_unit"
    fi
    # Remove unit files, masks/symlinks and per-unit overrides from standard
    # system locations. Leave the other applications and their data in place.
    for unit_dir in "${system_unit_dirs[@]}"; do
        rm -f -- "$unit_dir/$conflicting_unit"
        rm -rf -- "$unit_dir/$conflicting_unit.d"
    done
done

install -m 0644 "$render_dir/$unit_name" "/etc/systemd/system/$unit_name"
systemctl daemon-reload
systemctl enable "$unit_name"

if [[ "$start_service" == true ]]; then
    systemctl restart "$unit_name"
else
    echo "Installed and enabled $unit_name for boot; no start/restart was requested."
fi
echo "Configuration: $env_file"
echo "Python environment: $venv_dir"
if [[ "$added_dialout" == true ]]; then
    echo "Group membership is active for the service. Log out/in for existing manual terminals."
fi
echo "View logs: journalctl -u $unit_name -f"
