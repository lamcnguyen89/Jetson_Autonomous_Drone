# ubuntu turn off wifi and bluetooth command line


## Why disable Wifi and Bluetooth

For the Jetson mounted to the drone, the reason to disable the wifi and bluetooth is that these radio signals might cause interference with the Video, GPS, and RC signals transmitted from the drone to the pilot. With the commands below you can enable and disable Jetson TX.

## Procedure 

To disable Wi-Fi and Bluetooth from the command line in Ubuntu, you can use the **`rfkill`** utility or **`nmcli`** for network management.

**Using `rfkill` (Low-level Radio Control)**
This method blocks the radio hardware, which is effective for both Wi-Fi and Bluetooth.
1. List devices to identify IDs: `rfkill list`
2. Block all wireless devices: `sudo rfkill block all`
3. To re-enable later: `sudo rfkill unblock all`

**Using `nmcli` (NetworkManager)**
This method controls the Wi-Fi radio through the network manager service.
1. Turn off Wi-Fi: `nmcli radio wifi off`
2. Note: `nmcli` does not directly control Bluetooth; use `rfkill` or `systemctl` for Bluetooth (e.g., `sudo systemctl stop bluetooth`).

**Using `ip` Command**
To disable a specific Wi-Fi interface (e.g., `wlan0`):
`sudo ip link set wlan0 down`
This shuts down the interface but may not block the radio hardware entirely.