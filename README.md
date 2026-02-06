# Willowcrest Manor Audio Sync Glasses

This repository contains the code and setup scripts for a **synchronized personal audio system** used in the Willowcrest Manor immersive dining experience.  

Guests wear Bluetooth audio glasses paired to a small Raspberry Pi "puck". A central Master controller schedules and issues timestamped play commands to each puck over the LAN. Each puck stores WAV files locally and plays them via a lightweight player stack (aplay → paplay → mpv), ensuring group-wide synchronization (±20 ms).

## Repository Layout

- **/server**  
  Master controller web + WebSocket server (Python, aiohttp, websockets)  
  - `master.py` – Hosts an HTTP UI for device status, track upload, and "GO" button  
  - `requirements.txt` – Python dependencies  
  - `install_server.sh` – Automated installation & systemd setup on RPi 5

- **/client**  
  Raspberry Pi Zero W v1.1 or Zero 2 W puck client (Python, asyncio, mpv, bluetoothctl)  
  - `puck.py` – Connects to Master, schedules local playback, handles auto-pairing  
  - `requirements.txt` – Python dependencies  
  - `install_client.sh` – Automated installation & systemd setup on Pi Zero

## Key Features

- **Local audio playback**: Each puck stores WAV files locally and uses aplay/paplay/mpv for reliable output on Pi Zero
- **Timestamped scheduling**: Master issues commands with absolute timestamps; pucks align via time sync and drift checks
- **Auto-pairing & persistence**: Pucks scan and pair to the first discoverable glasses; remember MAC across reboots
- **Web-based control**: The themed UI includes a drag-and-drop timeline and a single COMMENCE button
- **Bluetooth stability tweaks**: Client install can apply Pi Zero firmware settings to reduce audio dropouts

## Getting Started
- **Master** (Windows or Linux): Clone the repo and follow the platform-specific instructions below.  
- **Clients**: see `client/README.md`.  

### Master (Windows PC)
1. Install Python 3.11+ from https://python.org.  
2. Open PowerShell and clone the repo:  
   ```powershell
   cd ~
   git clone https://github.com/samseidegames/audio_sync_glasses.git
   cd audio_sync_glasses/server
   ```
3. Create and activate a virtual environment:  
   ```powershell
   py -3 -m venv .venv
   . .venv\Scripts\Activate.ps1
   ```
4. Install dependencies:  
   ```powershell
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
5. Ensure your system clock is synced (Windows Time service).  
6. Run the master server:  
   ```powershell
   python master.py
   ```
7. Open a browser to http://willowcrestmanor.local:8080 (requires mDNS support) or http://<your_PC_hostname>:8080.  

_(Raspberry Pi instructions removed; use Windows or follow previous Pi instructions in `server/README.md`.)_

---

Built for theatrical infrastructure—reliable, scalable, and invisible to guests.  For full setup, see the subfolder READMEs.
