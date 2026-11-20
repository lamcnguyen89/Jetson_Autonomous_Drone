# MAVLink serial permissions

The project installer handles these permissions automatically. Set `SERVICE_USER`
and `MAVLINK_PORT` in the project root `.env`, connect the controller, then run
from the project root:

```bash
sudo ./scripts/Install_Auto_Drone.sh
```

The installer:

1. Adds `SERVICE_USER` to `dialout` without removing existing group memberships.
2. Resolves `MAVLINK_PORT`, including `/dev/serial/by-id/...` symlinks, and requires
   a connected tty character device.
3. Generates `/etc/udev/rules.d/99-auto-drone-mavlink.rules` with `GROUP="dialout"`
   and `MODE="0660"`. USB controllers match their vendor/product and serial
   identity (including the interface when available). A physical USB path is
   used when no usable serial number exists. An onboard UART such as
   `/dev/ttyTHS1` matches that fixed kernel name.
4. Reloads udev rules, reapplies them to the selected port, and waits for them to
   take effect.
5. Verifies that `SERVICE_USER` can read/write the port before installing/enabling
   the service. It does not open the port or send flight-controller commands.

The separate `80-auto-drone-oak.rules` grants camera access to the same group.
The service also uses `SupplementaryGroups=dialout`.

The service can use the permissions immediately. For commands run manually in
existing terminal sessions, log out and back in after adding the group. A reboot
is not required.

Re-run the installer if `MAVLINK_PORT` or `SERVICE_USER` changes. Prefer a
`/dev/serial/by-id/...` path for USB controllers so the service continues selecting
the same controller if its `/dev/ttyACM*` or `/dev/ttyUSB*` number changes. When a
rule uses a physical USB path, re-run the installer after moving the cable to a
different USB port. Add `--start` to the installer to start/restart the service
immediately.
