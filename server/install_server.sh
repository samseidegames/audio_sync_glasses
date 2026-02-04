#!/usr/bin/env bash
# install_server.sh
# Automates setup of Willowcrest Manor Audio Master on Raspberry Pi 5

set -e

# Prompt for Wi-Fi credentials
read -p "Enter Wi-Fi SSID: " SSID
read -s -p "Enter Wi-Fi PSK: " PSK
echo

# 1. System update and dependencies
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git linuxptp wpa_supplicant sshpass

# 2. Wi-Fi configuration
WPA_CONF=/etc/wpa_supplicant/wpa_supplicant.conf
# Append network block if not already configured
if ! grep -q "ssid=\"$SSID\"" "$WPA_CONF"; then
  echo "Configuring Wi-Fi network $SSID..."
  sudo tee -a "$WPA_CONF" > /dev/null <<EOF
network={
    ssid="$SSID"
    psk="$PSK"
    key_mgmt=WPA-PSK
}
EOF
  sudo wpa_cli -i wlan0 reconfigure
fi

# 3. Clone repository (if not exists)
REPO_DIR="$HOME/audio_sync_glasses"
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/samseidegames/audio_sync_glasses.git "$REPO_DIR"
fi

# 4. Python virtual environment
cd "$REPO_DIR/server"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Enable PTP grandmaster
sudo systemctl enable ptp4l
sudo systemctl start ptp4l

# 6. Create systemd service for master.py
SERVICE_FILE=/etc/systemd/system/audio-master.service
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Willowcrest Manor Audio Master
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR/server
ExecStart=$REPO_DIR/server/.venv/bin/python3 $REPO_DIR/server/master.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable audio-master.service
sudo systemctl start audio-master.service

echo "Server installation complete. UI available at http://willowcrestmanor.local:8080"
