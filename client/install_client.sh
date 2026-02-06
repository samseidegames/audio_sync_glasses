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

if [ $# -lt 1 ]; then
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
apt install -y python3 python3-venv python3-pip mpv bluetooth bluez bluez-tools wpasupplicant openntpd python3-rpi.gpio pulseaudio pulseaudio-utils pulseaudio-module-bluetooth

# Apply Bluetooth firmware tweaks for Pi Zero W / Zero 2 W to reduce audio dropouts
detect_model() {
  if [[ -f /proc/device-tree/model ]]; then
    tr -d '\0' < /proc/device-tree/model
  else
    echo ""
  fi
}

apply_bt_tweaks() {
  local fw_file="$1"
  if [[ -z "$fw_file" || ! -f "$fw_file" ]]; then
    echo "Bluetooth firmware file not found, skipping tweaks."
    return
  fi

  echo "Applying Bluetooth firmware tweaks to $fw_file"
  cp -n "$fw_file" "${fw_file}.bak" || true

  # Comment out legacy settings if present
  sed -i \
    -e 's/^btc_mode=/#btc_mode=/' \
    -e 's/^btc_params8=/#btc_params8=/' \
    -e 's/^btc_params1=/#btc_params1=/' \
    -e 's/^btc_params9=/#btc_params9=/' \
    -e 's/^btc_params50=/#btc_params50=/' \
    "$fw_file"

  # Append preferred settings
  cat <<'EOF' >> "$fw_file"
btc_mode=5
btc_params8=5000
btc_params9=40000
btc_params50=0x2000
EOF

  echo "Bluetooth firmware tweaks applied. A reboot is recommended."
}

MODEL=$(detect_model)
FW_ZERO_W="/lib/firmware/brcm/brcmfmac43430-sdio.raspberrypi,model-zero-w.txt"
FW_ZERO_2_W="/lib/firmware/brcm/brcmfmac43430b0-sdio.raspberrypi,model-zero-2-w.txt"

if [[ "$MODEL" == *"Zero 2"* ]]; then
  apply_bt_tweaks "$FW_ZERO_2_W"
elif [[ "$MODEL" == *"Zero W"* ]]; then
  apply_bt_tweaks "$FW_ZERO_W"
else
  echo "Unable to detect model automatically."
  echo "Select device type to apply Bluetooth firmware tweaks:"
  echo "  1) Raspberry Pi Zero W"
  echo "  2) Raspberry Pi Zero 2 W"
  echo "  3) Skip"
  read -rp "Choice [1-3]: " MODEL_CHOICE
  case "$MODEL_CHOICE" in
    1) apply_bt_tweaks "$FW_ZERO_W" ;;
    2) apply_bt_tweaks "$FW_ZERO_2_W" ;;
    *) echo "Skipping Bluetooth firmware tweaks." ;;
  esac
fi

# Configure Wi-Fi network if not present
WPA_CONF='/etc/wpa_supplicant/wpa_supplicant.conf'
if ! grep -qF "$SSID" "$WPA_CONF"; then
  echo "Configuring Wi-Fi network '$SSID'..."
  printf '\nnetwork={\n    ssid="%s"\n    psk="%s"\n    key_mgmt=WPA-PSK\n}\n' "$SSID" "$PSK" | tee -a "$WPA_CONF" > /dev/null
  wpa_cli -i wlan0 reconfigure
fi

# Determine script and repository directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

# 3. Repository location
# Using existing repository at $REPO_DIR/client

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
