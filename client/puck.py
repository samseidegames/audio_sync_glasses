#!/usr/bin/env python3
"""
Client audio puck for synchronized playback.
Runs on Raspberry Pi Zero W v1.1 and Zero 2 W. Connects to master via WebSocket, schedules audio playback locally.
Requires:
  - Python 3.7+
  - websockets 10.x
  - mpv installed with audio support
"""
import asyncio
import json
import time
import argparse
import subprocess
import sys
from pathlib import Path
import websockets
import RPi.GPIO as GPIO  # type: ignore
import re

# CONFIGURATION
SERVER_URI = 'ws://willowcrestmanor.local:8765'  # default server URI, can be overridden by --server argument
AUDIO_DIR = Path(__file__).parent / 'audio'
LOCAL_OFFSET = 0.0  # seconds (set via NTP/PTP externally)
CONFIG_PATH = Path(__file__).parent / 'paired_device.txt'

BUTTON_PIN = 17  # BCM pin number for discovery button

# Load saved MAC if exists
def load_paired_mac():
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text().strip()
    return None

# Save MAC to config
def save_paired_mac(mac: str):
    CONFIG_PATH.write_text(mac)

# Scan for devices, pair, trust, connect
def scan_and_pair():
    print("Scanning for discoverable Bluetooth devices...")
    # start bluetoothctl process
    proc = subprocess.Popen(['bluetoothctl'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # enable scanning
    proc.stdin.write('scan on\n')
    proc.stdin.flush()
    time.sleep(5)
    proc.stdin.write('scan off\n')
    proc.stdin.write('devices\n')
    proc.stdin.write('quit\n')
    out, _ = proc.communicate()
    # find Device lines
    for line in out.splitlines():
        m = re.match(r'Device ([0-9A-F:]{17})', line)
        if m:
            mac = m.group(1)
            print(f"Found device {mac}, pairing...")
            # pair, trust, connect
            cmds = f"pair {mac}\ntrust {mac}\nconnect {mac}\nquit\n"
            subprocess.run(['bluetoothctl'], input=cmds, text=True, check=False)
            return mac
    print("No discoverable devices found.")
    return None

# Ensure a paired device is connected
def ensure_paired_connection():
    mac = load_paired_mac()
    if mac:
        print(f"Attempting to connect to saved device {mac}...")
        res = subprocess.run(['bluetoothctl'], input=f"connect {mac}\nquit\n", text=True)
        if res.returncode == 0:
            print(f"Connected to {mac}")
            return
        else:
            print(f"Failed to connect to {mac}, will scan for new device.")
            CONFIG_PATH.unlink(missing_ok=True)
    # scan and pair new
    new_mac = scan_and_pair()
    if new_mac:
        save_paired_mac(new_mac)

async def sync_clock(samples: int = 5, timeout: float = 2.0, delay_between: float = 0.05):
    """
    Sync local clock to master via WebSocket time exchange.
    Protocol: send {"type":"TIME_REQUEST"}, expect {"type":"TIME_REPLY","server_time": <float>}
    Uses multiple samples and median offset to reduce jitter. Sets global LOCAL_OFFSET
    (server_time - local_time), so local_time + LOCAL_OFFSET ~= master_time.
    """
    global LOCAL_OFFSET
    offsets = []
    try:
        async with websockets.connect(SERVER_URI) as ws:
            for i in range(samples):
                t0 = time.time()
                await ws.send(json.dumps({"type": "TIME_REQUEST"}))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    print("Time sync: response timeout, retrying...")
                    continue
                t1 = time.time()
                try:
                    data = json.loads(msg)
                    if data.get("type") != "TIME_REPLY" or "server_time" not in data:
                        print(f"Time sync: unexpected reply: {data}")
                        continue
                    server_time = float(data["server_time"])
                except Exception as e:
                    print(f"Time sync: failed to parse reply: {e}")
                    continue
                rtt = t1 - t0
                offset_sample = server_time - (t0 + rtt / 2.0)
                offsets.append(offset_sample)
                await asyncio.sleep(delay_between)
    except Exception as e:
        print(f"Time sync: connection failed: {e}")

    if not offsets:
        print("Time sync: no valid samples, assuming external sync unchanged.")
        return

    # use median to reduce effect of outliers
    offsets.sort()
    mid = len(offsets) // 2
    if len(offsets) % 2 == 1:
        chosen = offsets[mid]
    else:
        chosen = (offsets[mid - 1] + offsets[mid]) / 2.0

    LOCAL_OFFSET = chosen
    print(f"Time sync complete: LOCAL_OFFSET set to {LOCAL_OFFSET:.6f} s (based on {len(offsets)} samples)")

async def send_registration(ws, guest_id):
    msg = {'guest_id': guest_id}
    await ws.send(json.dumps(msg))
    print(f"Registered with master as guest {guest_id}")

async def schedule_play(track_file, timestamp, offset):
    now = time.time() + LOCAL_OFFSET
    delay = timestamp - now
    if delay < 0:
        print(f"Missed start by {-delay:.3f}s, playing immediate with offset adjustment.")
        offset += -delay
        delay = 0
    await asyncio.sleep(delay)
    path = AUDIO_DIR / track_file
    if not path.exists():
        print(f"Error: track {path} not found.")
        return
    cmd = [
        'mpv', '--no-video', '--really-quiet',
        f'--start={offset}', str(path)
    ]
    print(f"Starting playback: {cmd}")
    subprocess.Popen(cmd)

async def handle_command(cmd):
    ctype = cmd.get('type')
    if ctype == 'PLAY':
        track = cmd['track']
        timestamp = cmd['timestamp']
        offset = cmd.get('offset', 0.0)
        asyncio.create_task(schedule_play(track, timestamp, offset))
    else:
        print(f"Unhandled command type: {ctype}")

async def listen(guest_id):
    async with websockets.connect(SERVER_URI) as ws:
        await send_registration(ws, guest_id)
        async for msg in ws:
            try:
                data = json.loads(msg)
                print(f"Command received: {data}")
                await handle_command(data)
            except json.JSONDecodeError:
                print(f"Invalid JSON: {msg}")

def enable_discovery():
    print("Enabling Bluetooth discovery mode...")
    cmd = "echo -e 'discoverable on\npairable on\nquit' | bluetoothctl"
    subprocess.run(cmd, shell=True, check=True)
    print("Bluetooth now discoverable and pairable")

def button_callback(channel):
    print("Discovery button pressed")
    enable_discovery()

def setup_button():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(BUTTON_PIN, GPIO.FALLING, callback=button_callback, bouncetime=300)

def main():
    parser = argparse.ArgumentParser(description='Audio Puck Client')
    parser.add_argument('--guest-id', required=True, help='Unique guest ID')
    parser.add_argument('--server', default=SERVER_URI, help='Master server URI')
    args = parser.parse_args()
    global SERVER_URI
    SERVER_URI = args.server

    # ensure audio directory exists
    if not AUDIO_DIR.exists():
        print(f"Creating audio directory at {AUDIO_DIR}")
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # auto-pair/connect glasses if needed
    ensure_paired_connection()
    # setup discovery button
    setup_button()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(sync_clock())
    try:
        loop.run_until_complete(listen(args.guest_id))
    except KeyboardInterrupt:
        print("Client shutting down.")
        sys.exit(0)

if __name__ == '__main__':
    main()
