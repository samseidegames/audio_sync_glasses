# Willowcrest Manor Audio Master - Server Setup

This guide covers installation and configuration of the master controller on a Raspberry Pi 5 (or equivalent Linux SBC).

## Prerequisites
- Raspberry Pi OS (64-bit) or Ubuntu Server
- Network access to local Wi-Fi (same LAN as pucks)
0. Wi-Fi Auto-Connect
    Configure `/etc/wpa_supplicant/wpa_supplicant.conf` with the following at the top (before any existing networks):
    ```ini
    network={
        ssid="iRouter"
        psk="norman17"
        key_mgmt=WPA-PSK
    }
    ```
    Reboot or run `sudo wpa_cli -i wlan0 reconfigure` to connect automatically on boot.
- PTP Grandmaster: `ptp4l` or `chrony` installed and configured externally

## 1. System Update & Dependencies
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

## 2. Clone & Install Python Packages
```bash
cd ~
git clone https://github.com/samseidegames/audio_sync_glasses.git
cd audio_sync_glasses/server
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Directory Structure
- `audio/` – Auto-created folder for uploaded MP3 files
- `master.py` – Main control server
- `requirements.txt` – Python dependencies

## 4. PTP Time Sync Setup
Ensure the Pi runs as PTP grandmaster:
```bash
sudo apt install linuxptp
# Example /etc/ptp4l.conf with masterOnly 1
sudo systemctl enable ptp4l
sudo systemctl start ptp4l
```

## 5. Starting the Server
```bash
source .venv/bin/activate
python3 master.py
```

- HTTP UI: http://willowcrestmanor.local:8080  
- WebSocket endpoint: ws://willowcrestmanor.local:8080/ws

## 6. Usage
1. Open the browser UI.  
2. Verify connected guests appear under **Connected Guests**.  
3. Upload and assign MP3 tracks per guest.  
4. Press **GO** to schedule synchronized playback.

## 7. (Optional) Autostart Service
Create a systemd service to run `master.py` on boot:
```ini
# /etc/systemd/system/audio-master.service
[Unit]
Description=Willowcrest Manor Audio Master
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/audio_sync_glasses/server
ExecStart=/home/pi/audio_sync_glasses/server/.venv/bin/python3 master.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable audio-master
sudo systemctl start audio-master
```
