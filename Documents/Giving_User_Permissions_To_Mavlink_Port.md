In order to get the user to have permissions to use the port for mavlink, you have to create a Udev rule

```bash
echo 'KERNEL=="ttyTHS1", GROUP="dialout", MODE="0660"' | sudo tee /etc/udev/rules.d/99-ttyTHS1.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG dialout malneyugnfl
sudo reboot
```