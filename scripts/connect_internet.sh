#!/usr/bin/env bash
# Restore Wi-Fi and automatic time synchronization after offline operation.

load_wifi_settings() {
    local settings
    [[ -f "$project_root/.env" ]] || {
        echo "Missing $project_root/.env; copy .env.example to .env and configure Wi-Fi." >&2
        return 1
    }
    settings=$(
        # Like the installer, source trusted, literal project configuration.
        # Use a subshell so unrelated settings stay out of the caller's shell.
        source "$project_root/.env" || exit 1
        # Python's standard library handles the JSON array, including spaces in
        # profile names. No project imports or virtual environment are needed.
        python3 - "${WIFI_CONNECTIONS:-}" "${WIFI_CONNECT_TIMEOUT:-90}" <<'PY'
import json
import re
import sys

try:
    profiles = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit('WIFI_CONNECTIONS must be a JSON array of saved profile names.')
if (not isinstance(profiles, list) or not profiles
        or any(not isinstance(p, str) or not p.strip()
               or any(c in p for c in '\r\n\0') for p in profiles)):
    raise SystemExit('WIFI_CONNECTIONS must be a nonempty JSON array of saved profile names.')
timeout = sys.argv[2]
if not re.fullmatch(r'[1-9][0-9]*', timeout):
    raise SystemExit('WIFI_CONNECT_TIMEOUT must be a positive integer.')
print(timeout)
print('\n'.join(profiles))
PY
    ) || return 1
    local -a values
    mapfile -t values <<< "$settings"
    CONNECT_TIMEOUT=${values[0]}
    WIFI_CONNECTIONS=("${values[@]:1}")
}

wifi_network_is_connected() {
    local ssid active_uuids uuid active_ssid state
    ssid=$(nmcli --get-values 802-11-wireless.ssid connection show id "$1" 2>/dev/null) || return 1
    [[ -n "$ssid" ]] || return 1
    active_uuids=$(nmcli --get-values UUID connection show --active 2>/dev/null) || return 1
    while IFS= read -r uuid; do
        [[ -n "$uuid" ]] || continue
        active_ssid=$(nmcli --get-values 802-11-wireless.ssid connection show uuid "$uuid" 2>/dev/null) || continue
        [[ "$active_ssid" == "$ssid" ]] || continue
        state=$(LC_ALL=C nmcli --get-values GENERAL.STATE connection show uuid "$uuid" 2>/dev/null) || continue
        [[ "$state" == activated ]] && return 0
    done <<< "$active_uuids"
    return 1
}

connect_wifi() {
    local network key_mgmt identity
    # Unblock every Wi-Fi radio, including USB adapters, before enabling NetworkManager.
    # A physical (hard) radio block still requires the device's switch or firmware.
    rfkill unblock wifi || return 1
    nmcli radio wifi on || return 1
    for network in "${WIFI_CONNECTIONS[@]}"; do
        # NetworkManager may use another saved profile for the same Wi-Fi SSID.
        if wifi_network_is_connected "$network"; then
            echo "Already connected to the Wi-Fi network for $network."
            return 0
        fi
        if ! key_mgmt=$(nmcli --get-values 802-11-wireless-security.key-mgmt connection show id "$network"); then
            echo "Saved profile '$network' is unavailable; trying the next network." >&2
            continue
        fi
        case "$key_mgmt" in
            wpa-eap|wpa-eap-suite-b-192|ieee8021x)
                identity=$(nmcli --get-values 802-1x.identity connection show id "$network") || continue
                if [[ -z "$identity" ]]; then
                    read -r -p "Enterprise username for $network: " identity || return 1
                    if [[ -z "$identity" ]]; then
                        echo "An enterprise username is required; trying the next network." >&2
                        continue
                    fi
                    nmcli connection modify id "$network" 802-1x.identity "$identity" || continue
                fi
                ;;
        esac
        echo "Connecting to $network; NetworkManager will prompt for missing passwords."
        # Let NetworkManager reuse saved secrets or prompt for credentials itself.
        if nmcli --ask --wait "$CONNECT_TIMEOUT" connection up id "$network"; then
            return 0
        fi
        # Activation can fail or time out while another profile connects.
        if wifi_network_is_connected "$network"; then
            echo "Connected to the Wi-Fi network for $network."
            return 0
        fi
        echo "Could not connect to $network; trying the next network." >&2
    done
    echo "Could not connect to any configured Wi-Fi profile. Check WIFI_CONNECTIONS in the root .env, adapter/profile compatibility, and rfkill list wifi for hardware blocks." >&2
    return 1
}

main() {
    set -euo pipefail
    local project_root
    project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

    (( EUID == 0 )) || { echo "Run this script with sudo." >&2; exit 1; }
    local command
    for command in nmcli rfkill timedatectl python3; do
        command -v "$command" >/dev/null || {
            echo "Required command is missing: $command" >&2
            exit 1
        }
    done
    [[ -t 0 ]] || {
        echo "Run in a terminal for Wi-Fi credential prompts." >&2
        exit 1
    }
    # Reject invalid configuration before changing NTP or radio state.
    load_wifi_settings

    echo "Enabling automatic time synchronization (NTP)..."
    timedatectl set-ntp true

    connect_wifi
    echo "Connected. NTP is enabled; synchronization may take time."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
