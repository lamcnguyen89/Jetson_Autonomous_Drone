#!/usr/bin/env bash
# The purpose of this file is to restore Wi-Fi and automatic time synchronization after running the drone services.
# Automatic Time Synchronization has to be turned off when no internet is available because it crashes the drone setup otherwise

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
    local command
    for command in nmcli rfkill; do
        command -v "$command" >/dev/null || {
            echo "Required command is missing: $command" >&2
            return 1
        }
    done
    [[ -t 0 ]] || {
        echo "Run in a terminal for Wi-Fi credential prompts." >&2
        return 1
    }
    local settings
    settings=$(python3 "$project_root/config/project_config.py" --shell wifi) || return 1
    eval "$settings"
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
    for command in nmcli rfkill timedatectl; do
        command -v "$command" >/dev/null || {
            echo "Required command is missing: $command" >&2
            exit 1
        }
    done

    echo "Enabling automatic time synchronization (NTP)..."
    timedatectl set-ntp true

    connect_wifi
    echo "Connected. NTP is enabled; synchronization may take time."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
