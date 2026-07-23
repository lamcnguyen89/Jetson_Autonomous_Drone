Here is the complete step-by-step workflow to create, enable, and test your systemd service on Ubuntu.

---

## Step 1: Create the Service Unit File

Create and open a new unit file in systemd's system folder using `nano`:

```bash
sudo nano /etc/systemd/system/stanley-drone.service

```

Paste your corrected configuration into the editor:

```ini
[Unit]
Description=Stanley follow-me startup script
After=network.target

[Service]
Environment="HOME=/root"
WorkingDirectory=/home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/
ExecStart=/usr/local/bin/python3.9 /home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/src/main.py --uri /dev/ttyTHS1 --video --video_path /home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/videos/ --killswitch_path /home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/killswitch_location/ --log_path /home/malneyugnfl/Documents/SoftwareDev/Jetson_Autonomous_Drone/logs/
StandardOutput=inherit
StandardError=inherit
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target

```

Press `Ctrl + O`, then `Enter` to save, and `Ctrl + X` to exit.

---

## Step 2: Set File Permissions

Ensure the file is owned by `root` and readable by systemd:

```bash
sudo chmod 644 /etc/systemd/system/stanley-drone.service

```

---

## Step 3: Register and Enable the Service

Run the following commands to register the new file with systemd and set it to start automatically at boot:

1. **Reload the systemd manager:** Run after making changes to unit files.
Forces systemd to scan `/etc/systemd/system/` and pick up your new service file.

```bash
sudo systemctl daemon-reload

```


2. **Enable boot auto-start:** Registers service with multi-user target.
Creates the necessary symlinks so Ubuntu launches the script on startup.

```bash
sudo systemctl enable stanley-drone.service

```


3. **Start the service immediately:** Test without rebooting.
Launches your Python script right away in the background.

```bash
sudo systemctl start stanley-drone.service

```


---

## Step 4: Verify Service Status and Logs

To check if your Python script is actively running or if it crashed on startup:

* **Check runtime status:**
```bash
sudo systemctl status stanley-drone.service

```


*(Look for a green `active (running)` status. If it says `failed` or `auto-restart`, proceed to log checking).*
* **Stream live console logs (`stdout` / `stderr`):**
```bash
sudo journalctl -u stanley-drone.service -f

```


*(Press `Ctrl + C` to exit log streaming).*

---

### Useful Controlling Commands

| Command | Action |
| --- | --- |
| `sudo systemctl stop stanley-drone.service` | Stops the running script |
| `sudo systemctl restart stanley-drone.service` | Forces a fresh restart of the script |
| `sudo systemctl disable stanley-drone.service` | Prevents the script from auto-starting on boot |