#!/usr/bin/env bash
# Disable automatic time synchronization and all Wi-Fi radios for connectivity tests.

disable_ntp() {
    timeout 30s timedatectl set-ntp false || return 1
    # timedatectl can return before the time synchronization service has stopped.
    local deadline=$((SECONDS + 30)) remaining state
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        state=$(timeout "${remaining}s" timedatectl show --property=NTP --value) || return 1
        case "${state,,}" in
            no|false) return 0 ;;
        esac
        sleep 1
    done
    echo "NTP did not become disabled within 30 seconds." >&2
    return 1
}

disable_internet() {
    local failed=0
    echo "Disabling automatic time synchronization (NTP)..."
    if ! disable_ntp; then
        echo "Could not verify that NTP is disabled." >&2
        failed=1
    fi

    echo "Disabling all Wi-Fi radios (this disconnects Wi-Fi sessions)..."
    # Try both controls even if one fails. 'wifi' leaves Bluetooth/WWAN alone.
    if ! nmcli radio wifi off; then
        echo "Could not disable Wi-Fi in NetworkManager." >&2
        failed=1
    fi
    if ! rfkill block wifi; then
        echo "Could not block Wi-Fi radios with rfkill." >&2
        failed=1
    fi
    (( failed == 0 )) || return 1
    echo "NTP is disabled and Wi-Fi radios are software-blocked. Ethernet is unchanged."
}

main() {
    set -euo pipefail
    (( EUID == 0 )) || { echo "Run this script with sudo." >&2; exit 1; }
    local command
    for command in nmcli rfkill timedatectl timeout; do
        command -v "$command" >/dev/null || {
            echo "Required command is missing: $command" >&2
            exit 1
        }
    done
    disable_internet
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi