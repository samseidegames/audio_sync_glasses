# Willowcrest Manor Audio Puck - Client Setup

Instructions for setting up each Raspberry Pi Zero W v1.1 or Zero 2 W as an audio puck.

## Prerequisites
- Raspberry Pi OS Lite installed on SD card
0. Wi-Fi Auto-Connect
  Configure `/etc/wpa_supplicant/wpa_supplicant.conf` with:
  ```ini
  country=US
  ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
  update_config=1

  network={
      ssid="iRouter"
      psk="********"
      key_mgmt=WPA-PSK
  }
  ```
  Then reboot or run `sudo wpa_cli -i wlan0 reconfigure` to apply.
- Wi-Fi credentials for control network
- MPV media player installed

## 1. System Update & Dependencies
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip mpv bluetooth bluez bluez-tools python3-rpi.gpio
```

## 2. Run Install Script
```bash
cd ~/audio_sync_glasses/client
sudo bash install_client.sh <guest_id>
```

## 3. Bluetooth Pairing (One-time)
1. Enable pairing mode on puck:
   ```bash
   rfkill unblock all
   bluetoothctl
   discoverable on
   pairable on
   ```
2. On the host PC or device, use `bluetoothctl` to scan for devices and pair with the guest’s Bluetooth audio glasses:
   ```bash
   bluetoothctl
   scan on
   # Wait for your device to appear, then:
   pair XX:XX:XX:XX:XX:XX
   ```
   Replace `XX:XX:XX:XX:XX:XX` with the MAC address of the Bluetooth audio glasses.

On the Pi Zero W v1.1 or Zero 2 W (in bluetoothctl with discoverable/pairable on), put the glasses into their manufacturer pairing mode and run:
```bash
# inside bluetoothctl on the Pi Zero W v1.1 or Zero 2 W
scan on              # watch for "Device XX:XX:XX:XX:XX:XX <NAME>"
# when you see the glasses' MAC, note it and run:
scan off
```
After you have the glasses' MAC, continue to step 3 to trust and connect.

Note: the server (master) always runs on a Raspberry Pi 5. The server web UI (http://<RPI5_IP>:8080/) will show, for each Pi Zero W v1.1 or Zero 2 W puck:
- whether the puck is connected to the server,
- whether that puck currently has Bluetooth glasses connected, and
- the glasses' remaining battery (if the glasses report battery and the client forwards it to the server).
3. Trust and connect:
   ```bash
   trust XX:XX:XX:XX:XX:XX
   connect XX:XX:XX:XX:XX:XX
   ```
4. Exit `bluetoothctl`.
5. Label the puck & glasses (e.g., Guest 1).

## 4. Directory Structure
- `audio/` – Place per-guest MP3 files named `<guest_id>.mp3` or upload from master.
- `puck.py` – Main client script.
- `requirements.txt` – Python dependencies.

## 5. Time Sync Setup
Use PTP or an NTP daemon (openntpd) to sync clock with master:
```bash
sudo apt install openntpd
sudo systemctl enable openntpd
sudo systemctl start openntpd
```

## 6. Running the Client
```bash
source .venv/bin/activate
# Run as root (with sudo) to allow GPIO access for button support
sudo python3 puck.py --guest-id 1 --server ws://<MASTER_IP>:8080/ws
```

## 7. Button for Pairing
Press the physical button (GPIO17) on the puck to re-enable Bluetooth discovery mode.

## 8. (Optional) Autostart on Boot
Add to `/etc/rc.local` before `exit 0`:
```bash
su pi -c 'cd /home/pi/audio_sync_glasses/client && \
  source .venv/bin/activate && \
  python3 puck.py --guest-id 1 --server ws://<MASTER_IP>:8080/ws >> puck.log 2>&1 &'
```
