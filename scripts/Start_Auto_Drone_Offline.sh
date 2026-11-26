#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: sudo $0"
    echo "Install, enable at boot, and start/restart Auto_Drone.service offline."
    echo "Requires an existing .env, .venv, dependencies, models and device permissions."
    echo "Does not install packages, download models, or change competing services."
}

if (( $# )); then
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="$project_root/.env"
venv_python="$project_root/.venv/bin/python"
unit_name=Auto_Drone.service

if (( EUID != 0 )); then
    echo "Run this script with sudo to install and start the systemd unit." >&2
    exit 1
fi
for command in systemctl systemd-analyze install runuser; do
    if ! command -v "$command" >/dev/null; then
        echo "Required local command is missing: $command. No packages were installed." >&2
        exit 1
    fi
done
if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file. Configure the project before starting offline." >&2
    exit 1
fi
if [[ ! -f "$project_root/.venv/pyvenv.cfg" || ! -x "$venv_python" ]]; then
    echo "An existing project .venv is required. No dependencies were installed." >&2
    exit 1
fi

# .env is trusted project configuration, also sourced by the full installer.
# Keep settings literal, as documented for Bash and systemd compatibility.
unset SERVICE_USER MAVLINK_PORT
source "$env_file"
: "${SERVICE_USER:?Set SERVICE_USER in .env}"
: "${MAVLINK_PORT:?Set MAVLINK_PORT in .env}"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Service user does not exist: $SERVICE_USER" >&2
    exit 1
fi
if [[ "$MAVLINK_PORT" != /dev/* ]]; then
    echo "MAVLINK_PORT must be a device path under /dev/." >&2
    exit 1
fi
for file in "$env_file" "$project_root/src/main.py"; do
    if ! runuser -u "$SERVICE_USER" -- test -r "$file"; then
        echo "$SERVICE_USER cannot read $file." >&2
        exit 1
    fi
done
if ! runuser -u "$SERVICE_USER" -- test -x "$venv_python"; then
    echo "$SERVICE_USER cannot execute $venv_python." >&2
    exit 1
fi

# Render and validate locally before changing the installed service. Do not
# invoke the full installer, application, or model preparation during setup.
render_dir="$(mktemp -d "$project_root/scripts/.offline.XXXXXX")"
trap 'rm -rf -- "$render_dir"' EXIT
"$venv_python" - "$project_root" "$SERVICE_USER" "$render_dir/$unit_name" <<'PY'
from pathlib import Path
import sys

project_root, service_user, output = sys.argv[1:]

def systemd_escape(value):
    if '\n' in value or '\r' in value:
        raise ValueError('The project path and service user must not contain newlines')
    return value.replace('%', '%%')

template = (Path(project_root) / 'systemd/Auto_Drone.service').read_text()
rendered = template.replace('@PROJECT_ROOT@', systemd_escape(project_root))
rendered = rendered.replace('@SERVICE_USER@', systemd_escape(service_user))
Path(output).write_text(rendered)
PY
systemd-analyze verify "$render_dir/$unit_name"
install -m 0644 "$render_dir/$unit_name" "/etc/systemd/system/$unit_name"
systemctl daemon-reload
systemctl enable "$unit_name"
systemctl restart "$unit_name"
systemctl is-active --quiet "$unit_name"

echo "Installed, enabled and started $unit_name using existing local dependencies."
echo "Configuration: $env_file"
echo "View logs: journalctl -u $unit_name -f"
