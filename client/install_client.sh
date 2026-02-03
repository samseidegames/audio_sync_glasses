#!/usr/bin/env bash
# install_client.sh
# Automates setup of Willowcrest Manor Audio Puck on Raspberry Pi Zero W v1.1 and Zero 2 W
# Usage: sudo bash install_client.sh <guest_id>

set -euo pipefail

# Must run as root
if [[ $EUID -ne 0 ]]; then
  echo "Please run as root or with sudo"
  exit 1
fi

if [ -z "$1" ]; then
  echo "Usage: sudo bash install_client.sh <guest_id>"
  exit 1
fi
GUEST_ID=$1

# Prompt for Wi-Fi credentials
read -rp "Enter Wi-Fi SSID: " SSID
read -srp "Enter Wi-Fi PSK: " PSK
echo

# 1. System update and dependencies
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip mpv bluetooth bluez bluez-tools wpasupplicant openntpd

# Configure Wi-Fi network if not present
WPA_CONF='/etc/wpa_supplicant/wpa_supplicant.conf'
if ! grep -qF "$SSID" "$WPA_CONF"; then
  echo "Configuring Wi-Fi network '$SSID'..."
  printf '\nnetwork={\n    ssid="%s"\n    psk="%s"\n    key_mgmt=WPA-PSK\n}\n' "$SSID" "$PSK" | tee -a "$WPA_CONF" > /dev/null
  wpa_cli -i wlan0 reconfigure
fi

# 3. Clone repository (if not exists)
REPO_DIR="/home/pi/audio_sync_glasses"
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/samseidegames/audio_sync_glasses.git "$REPO_DIR"
fi

# 4. Python virtual environment
cd "$REPO_DIR/client" || exit 1
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

# 5. Enable and start NTP
systemctl enable openntpd
systemctl start openntpd

# 6. Create systemd service for puck.py
SERVICE_FILE='/etc/systemd/system/audio-puck.service'
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Willowcrest Manor Audio Puck (Guest $GUEST_ID)
After=network-online.target bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=$REPO_DIR/client
# Use bash login shell to activate venv and run client
ExecStart=/bin/bash -lc 'cd $REPO_DIR/client && . .venv/bin/activate && python3 puck.py --guest-id $GUEST_ID --server ws://willowcrestmanor.local:8080/ws'
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable audio-puck.service
systemctl start audio-puck.service

echo "Client installation complete. Puck running as guest ID $GUEST_ID."
