#!/usr/bin/env python3
"""
Trigger client for synchronized event triggering.
Runs on Raspberry Pi Zero W v1.1 and Zero 2 W. Connects to master via WebSocket.
Receives trigger events and executes local routines (LED control, servos, etc.)
Requires:
  - Python 3.7+
  - websockets 10.x
  - RPi.GPIO (optional, for GPIO control)
"""
import asyncio
import json
import time
import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime
import websockets

def format_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')[:-3]

# CONFIGURATION
SERVER_URI = 'ws://willowcrestmanor.local:8765'  # default server URI
LOCAL_OFFSET = 0.0  # seconds (set via NTP/PTP externally)
CONFIG_PATH = Path(__file__).parent / 'trigger_config.txt'

# Load saved trigger ID if exists
def load_trigger_id():
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text().strip()
    return None

# Save trigger ID to config
def save_trigger_id(trigger_id: str):
    CONFIG_PATH.write_text(trigger_id)

# Define custom trigger routines here
# These will be called when a trigger event is received
TRIGGER_ROUTINES = {
    'led_on': lambda: print("[TRIGGER] LED ON"),
    'led_off': lambda: print("[TRIGGER] LED OFF"),
    'servo_move': lambda: print("[TRIGGER] SERVO MOVE"),
    'pulse': lambda: print("[TRIGGER] PULSE"),
}

def execute_trigger(trigger_name):
    """Execute a trigger routine by name."""
    if trigger_name in TRIGGER_ROUTINES:
        try:
            print(f"[{format_time(time.time())}] Executing trigger: {trigger_name}")
            TRIGGER_ROUTINES[trigger_name]()
        except Exception as e:
            print(f"[{format_time(time.time())}] Error executing trigger {trigger_name}: {e}")
    else:
        print(f"[{format_time(time.time())}] Unknown trigger: {trigger_name}")

async def send_registration(ws, trigger_id):
    """Register this trigger with the master server."""
    msg = {'trigger_id': trigger_id}
    await ws.send(json.dumps(msg))
    print(f"Registered with master as trigger {trigger_id}")

async def handle_message(msg_data):
    """Handle incoming message from server."""
    if msg_data.get('type') == 'TRIGGER_EVENT':
        trigger_name = msg_data.get('trigger')
        timestamp = float(msg_data.get('timestamp', time.time()))
        # Check if we should execute now or wait
        now = time.time() + LOCAL_OFFSET
        delay = timestamp - now
        if delay > 0.01:  # Small threshold to avoid timing issues
            await asyncio.sleep(delay)
        execute_trigger(trigger_name)

async def connect_to_server(server_uri, trigger_id):
    """Connect to the master server and handle messages."""
    try:
        async with websockets.connect(server_uri) as ws:
            print(f"Connected to server at {server_uri}")
            
            # Register
            await send_registration(ws, trigger_id)
            
            # Listen for messages
            try:
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        await handle_message(data)
                    except json.JSONDecodeError:
                        print(f"Invalid JSON received: {msg}")
                    except Exception as e:
                        print(f"Error handling message: {e}")
            except asyncio.CancelledError:
                print("Connection closed")
    except Exception as e:
        print(f"Connection error: {e}")
        print(f"Retrying in 5 seconds...")
        await asyncio.sleep(5)

async def main():
    parser = argparse.ArgumentParser(description='Trigger client for audio sync glasses')
    parser.add_argument('--server', default=SERVER_URI, help='Server WebSocket URI')
    parser.add_argument('--trigger-id', help='Trigger ID (default: saved config or hostname)')
    args = parser.parse_args()
    
    # Determine trigger ID
    trigger_id = args.trigger_id or load_trigger_id()
    if not trigger_id:
        # Use hostname as default
        trigger_id = subprocess.run(['hostname', '-s'], capture_output=True, text=True).stdout.strip()
        if not trigger_id:
            trigger_id = f"trigger-{int(time.time())}"
    
    save_trigger_id(trigger_id)
    print(f"Trigger ID: {trigger_id}")
    print(f"Server: {args.server}")
    print(f"Available triggers: {', '.join(TRIGGER_ROUTINES.keys())}")
    print("Waiting for commands from server...")
    
    # Connect and reconnect on failure
    while True:
        try:
            await connect_to_server(args.server, trigger_id)
        except Exception as e:
            print(f"Unexpected error: {e}")
            await asyncio.sleep(5)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
