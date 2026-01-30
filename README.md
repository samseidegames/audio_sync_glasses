# Willowcrest Manor Audio Sync Glasses

This repository contains the code and setup scripts for a **synchronized personal audio system** used in the Willowcrest Manor immersive dining experience.  

Guests wear Bluetooth audio glasses paired to a small Raspberry Pi "puck".  A central Master controller schedules and issues timestamped play/stop/fade commands to each puck over the LAN.  Each puck streams private MP3 audio to its paired glasses, ensuring group-wide synchronization (±20 ms).

## Repository Layout

- **/server**  
  Master controller web + WebSocket server (Python, aiohttp, websockets)  
  - `master.py` – Hosts an HTTP UI for device status, track upload, and "GO" button  
  - `requirements.txt` – Python dependencies  
  - `install_server.sh` – Automated installation & systemd setup on RPi 5

- **/client**  
  Raspberry Pi Zero 2 W puck client (Python, asyncio, mpv, bluetoothctl)  
  - `puck.py` – Connects to Master, schedules local playback, handles auto-pairing  
  - `requirements.txt` – Python dependencies  
  - `install_client.sh` – Automated installation & systemd setup on Pi Zero

## Key Features

- **Local audio playback**: Each puck stores MP3 files locally and uses MPV for precise seeks and fades
- **Timestamped scheduling**: Master issues commands with absolute timestamps; pucks align via NTP/PTP
- **Auto-pairing & persistence**: Pucks scan and pair to the first discoverable glasses; remember MAC across reboots
- **Web-based control**: Staff never touch menus — just upload tracks in the browser and click "GO"

## Getting Started
1. **Master**:  Clone the repo on a Raspberry Pi 5 and run `install_server.sh`.  Reboot, then open http://willowcrestmanor.local:8080  
2. **Clients**:  Flash Pi Zero images, clone repo on each, then `sudo install_client.sh <guest_id>`.  Reboot to auto-start.

---

Built for theatrical infrastructure—reliable, scalable, and invisible to guests.  For full setup, see the subfolder READMEs.
