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
from datetime import datetime
import websockets
import RPi.GPIO as GPIO  # type: ignore
import re
import urllib.request

# Try to import mutagen for WAV duration detection; fall back gracefully
try:
    from mutagen.wave import WAVE
except Exception:
    WAVE = None
    MP3 = None
    WAVE = None


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')[:-3]

# CONFIGURATION
SERVER_URI = 'ws://willowcrestmanor.local:8765'  # default server URI, can be overridden by --server argument
AUDIO_DIR = Path(__file__).parent / 'audio'
LOCAL_OFFSET = 0.0  # seconds (set via NTP/PTP externally)
CONFIG_PATH = Path(__file__).parent / 'paired_device.txt'

# scheduler state for logging delays
scheduled_queue = []  # list of dicts {'track','timestamp','offset'} sorted by timestamp
last_expected_end_time = None  # server-timestamp of when last scheduled track is expected to finish
active_plays = 0  # count of currently playing processes

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
    # Format offset as HH:MM:SS for display
    offset_hours = int(LOCAL_OFFSET // 3600)
    offset_mins = int((LOCAL_OFFSET % 3600) // 60)
    offset_secs = LOCAL_OFFSET % 60
    print(f"Time sync complete: LOCAL_OFFSET set to {offset_hours:02d}:{offset_mins:02d}:{offset_secs:06.3f} (based on {len(offsets)} samples)")

async def send_registration(ws, guest_id):
    # include client installation directory for file uploads
    install_dir = str(Path(__file__).parent)
    msg = {'guest_id': guest_id, 'install_dir': install_dir, 'local_offset': LOCAL_OFFSET}
    await ws.send(json.dumps(msg))
    print(f"Registered with master as guest {guest_id} (install_dir={install_dir})")

def get_track_duration(track_file):
    """Return duration in seconds for a WAV audio file, 0.0 if unknown."""
    path = AUDIO_DIR / track_file
    if not path.exists():
        download_track(track_file)
    if not path.exists():
        if WAVE is None:
            print("Warning: mutagen not available; cannot determine track duration.")
        return 0.0
    try:
        # All files are now WAV (converted on upload server)
        if track_file.lower().endswith('.wav') and WAVE:
            audio = WAVE(str(path))
            return audio.info.length
        else:
            # Fallback to ffprobe
            try:
                result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                       '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                                      capture_output=True, text=True, timeout=5)
                return float(result.stdout.strip())
            except Exception:
                return 0.0
    except Exception as e:
        print(f"Warning: failed to read duration for {track_file}: {e}")
        return 0.0


def download_track(track_file):
    """Download track from server HTTP audio endpoint"""
    try:
        # Construct HTTP URL from SERVER_URI
        base = SERVER_URI.replace('ws://', 'http://').rstrip('/ws')
        url = f"{base}/audio/{track_file}"
        dest = AUDIO_DIR / track_file
        print(f"[{format_time(time.time())}] Downloading track from {url} to {dest}")
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, dest)
        print(f"[{format_time(time.time())}] Downloaded {track_file}")
    except Exception as e:
        print(f"[{format_time(time.time())}] Failed to download {track_file}: {e}")

async def schedule_play(track_file, timestamp, offset):
    global last_expected_end_time, scheduled_queue, active_plays
    try:
        # Refresh clock sync before scheduling playback to reduce drift
        # Light-weight drift check instead of full re-sync before each track to reduce jitter
        now = time.time() + LOCAL_OFFSET
        delay = timestamp - now
        if delay < 0:
            delay_hours = int((-delay) // 3600)
            delay_mins = int(((-delay) % 3600) // 60)
            delay_secs = (-delay) % 60
            print(f"[{format_time(time.time())}] Missed start by {delay_hours:02d}:{delay_mins:02d}:{delay_secs:06.3f}, playing immediate with offset adjustment.")
            offset += -delay
            delay = 0
        
        print(f"[{format_time(time.time())}] Scheduling playback: waiting {delay:.2f}s for track '{track_file}' (ts={timestamp}, offset={offset})")
        await asyncio.sleep(delay)
        
        # Check file exists
        path = AUDIO_DIR / track_file
        print(f"[{format_time(time.time())}] Looking for audio file: {path}")
        print(f"[{format_time(time.time())}] File exists: {path.exists()}")
        
        if not path.exists():
            print(f"[{format_time(time.time())}] File not found locally, attempting download...")
            download_track(track_file)
            print(f"[{format_time(time.time())}] After download, file exists: {path.exists()}")
            if not path.exists():
                print(f"[{format_time(time.time())}] ERROR: track {path} not found after download.")
                return
        
        # Use aplay for lightweight WAV playback on Raspberry Pi (preferred for WAV)
        # Falls back to paplay (PulseAudio) or mpv if aplay unavailable
        
        # Try aplay first (native ALSA, works best for WAV on Pi)
        cmd = None
        player = None
        
        # Check which player is available
        try:
            subprocess.run(['which', 'aplay'], capture_output=True, check=True, timeout=2)
            # aplay is available - use it for WAV playback
            cmd = ['aplay', str(path)]
            player = 'aplay'
            print(f"[{format_time(time.time())}] Starting playback (aplay): {track_file}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            # aplay not available, try paplay
            try:
                subprocess.run(['which', 'paplay'], capture_output=True, check=True, timeout=2)
                cmd = [
                    'paplay', '--device=default', str(path)
                ]
                player = 'paplay'
                print(f"[{format_time(time.time())}] Starting playback (paplay): {track_file}")
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Fall back to mpv
                cmd = [
                    'mpv', '--no-video', '--really-quiet', '--audio-device=default',
                    '--cache=no', f'--start={offset}', str(path)
                ]
                player = 'mpv'
                print(f"[{format_time(time.time())}] Starting playback (mpv): {track_file} (offset {offset}s)")
        
        if cmd is None:
            print(f"[{format_time(time.time())}] ERROR: No audio player available!")
            return
        
        # start actual playback and track active plays
        start_actual = time.time() + LOCAL_OFFSET
        active_plays += 1
        try:
            # Capture stderr to see audio errors
            with open('/tmp/audio_playback.log', 'a') as log:
                log.write(f"[{format_time(start_actual)}] Starting {player} with cmd: {cmd}\n")
            print(f"[{format_time(time.time())}] Executing command: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            print(f"[{format_time(time.time())}] Process started (PID: {proc.pid})")
        except Exception as e:
            print(f"[{format_time(time.time())}] ERROR starting playback: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            active_plays -= 1
            return
    except Exception as e:
        print(f"[{format_time(time.time())}] CRITICAL ERROR in schedule_play: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # remove this play from scheduled_queue (match by track and timestamp) and capture duration from server if present
    duration = None
    try:
        for i, s in enumerate(list(scheduled_queue)):
            if s.get('track') == track_file and abs(s.get('timestamp', 0) - timestamp) < 0.001:
                duration = float(s.get('duration', 0) or 0)
                scheduled_queue.pop(i)
                break
    except Exception:
        pass

    # wait for playback process to complete
    try:
        await asyncio.get_running_loop().run_in_executor(None, proc.wait)
        _, stderr = proc.communicate()
        if stderr:
            with open('/tmp/audio_playback.log', 'a') as log:
                log.write(f"[{format_time(time.time())}] Audio player stderr: {stderr}\n")
            print(f"[{format_time(time.time())}] Audio player error output: {stderr}")
        end_actual = time.time() + LOCAL_OFFSET
        # compute duration (fallback if server didn't provide)
        if duration is None or duration == 0:
            duration = get_track_duration(track_file)
        # Format durations as HH:MM:SS
        dur_hours = int(duration // 3600)
        dur_mins = int((duration % 3600) // 60)
        dur_secs = duration % 60
        print(f"[{format_time(end_actual)}] Playback finished for {track_file} (duration: {dur_hours:02d}:{dur_mins:02d}:{dur_secs:06.3f}, offset: {offset:.1f}s)")
        last_expected_end_time = end_actual
        active_plays -= 1
        # if there is a next scheduled play, compute delay and report relative to actual end
        next_item = None
        for s in scheduled_queue:
            if s.get('timestamp', 0) >= end_actual - 0.0001:
                next_item = s
                break
        if next_item:
            delay_seconds = next_item.get('timestamp', 0) - end_actual
            if delay_seconds > 0:
                print(f"[{format_time(end_actual)}] Delay: {delay_seconds:.1f}s before next track ({next_item.get('track')})")
    except Exception as e:
        print(f"[{format_time(time.time())}] ERROR during playback wait: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        active_plays -= 1
    finally:
        try:
            # Ensure process is terminated if still running
            if 'proc' in locals() and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except:
                    proc.kill()
        except:
            pass

async def handle_command(cmd):
    ctype = cmd.get('type')
    if ctype == 'PLAY':
        track = cmd['track']
        timestamp = float(cmd['timestamp'])
        offset = float(cmd.get('offset', 0.0))
        duration = float(cmd.get('duration', 0.0))
        print(f"[{format_time(time.time())}] Processing PLAY command for {track}")
        # Format duration for log output as HH:MM:SS
        dur_hours = int(duration // 3600)
        dur_mins = int((duration % 3600) // 60)
        dur_secs = duration % 60
        # compute expected finish for this item
        expected_finish = timestamp + duration
        # Log scheduled gap vs previous expected end
        global scheduled_queue, last_expected_end_time
        if last_expected_end_time is not None and timestamp > last_expected_end_time + 0.001:
            gap = timestamp - last_expected_end_time
            # Format gap as HH:MM:SS
            gap_hours = int(gap // 3600)
            gap_mins = int((gap % 3600) // 60)
            gap_secs = gap % 60
            print(f"[{format_time(time.time())}] Scheduled delay of {gap_hours:02d}:{gap_mins:02d}:{gap_secs:06.3f} before {track}")
        # add to scheduled queue (keep sorted) and include duration
        scheduled_queue.append({'track': track, 'timestamp': timestamp, 'offset': offset, 'duration': duration})
        scheduled_queue.sort(key=lambda x: x['timestamp'])
        # update last_expected_end_time to the max of known scheduled finishes
        if last_expected_end_time is None:
            last_expected_end_time = expected_finish
        else:
            last_expected_end_time = max(last_expected_end_time, expected_finish)
        
        # Create task with error callback to catch any exceptions
        task = asyncio.create_task(schedule_play(track, timestamp, offset))
        def handle_task_error(t):
            try:
                t.result()
            except Exception as e:
                print(f"[{format_time(time.time())}] Task error for {track}: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
        task.add_done_callback(handle_task_error)
    elif ctype == 'PLAYLIST_COMPLETE':
        ts = float(cmd.get('timestamp', 0))
        async def log_playlist_complete(at_ts):
            # Wait until server time reaches the declared playlist end AND no active plays remain
            while True:
                now = time.time() + LOCAL_OFFSET
                if now >= at_ts and active_plays == 0 and not scheduled_queue:
                    break
                await asyncio.sleep(0.1)
            # Log at the observed completion time
            observed = time.time() + LOCAL_OFFSET
            print(f"[{format_time(observed)}] Playlist Complete")
        asyncio.create_task(log_playlist_complete(ts))
    elif ctype == 'SHOW_COMPLETE':
        ts = float(cmd.get('timestamp', 0))
        async def log_show_complete(at_ts):
            # Wait until server time reaches the declared show end AND no active plays remain
            while True:
                now = time.time() + LOCAL_OFFSET
                if now >= at_ts and active_plays == 0 and not scheduled_queue:
                    break
                await asyncio.sleep(0.1)
            observed = time.time() + LOCAL_OFFSET
            print(f"[{format_time(observed)}] Show Complete")
        asyncio.create_task(log_show_complete(ts))
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
    try:
        GPIO.add_event_detect(BUTTON_PIN, GPIO.FALLING, callback=button_callback, bouncetime=300)
    except RuntimeError as e:
        print(f"GPIO button setup skipped: {e}")

def main():
    global SERVER_URI
    parser = argparse.ArgumentParser(description='Audio Puck Client')
    parser.add_argument('--guest-id', required=True, help='Unique guest ID')
    parser.add_argument('--server', default=SERVER_URI, help='Master server URI')
    args = parser.parse_args()
    SERVER_URI = args.server

    # ensure audio directory exists
    if not AUDIO_DIR.exists():
        print(f"Creating audio directory at {AUDIO_DIR}")
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # auto-pair/connect glasses if needed
    ensure_paired_connection()

    # start PulseAudio and configure Bluetooth audio if not running as root
    import os
    if os.geteuid() != 0:
        try:
            subprocess.run(['pulseaudio', '--start'], check=False)
            subprocess.run(['pactl', 'load-module', 'module-bluetooth-discover'], check=False)
            subprocess.run(['pactl', 'load-module', 'module-bluetooth-policy'], check=False)
            mac = load_paired_mac()
            if mac:
                sink = f'bluez_sink.{mac.replace(":", "_")}.a2dp_sink'
                print(f'Setting default PulseAudio sink: {sink}')
                subprocess.run(['pactl', 'set-default-sink', sink], check=False)
        except Exception as e:
            print(f"PulseAudio setup skipped: {e}")
    else:
        print("PulseAudio setup skipped: running as root; use pi user session for audio output.")

    # setup discovery button
    setup_button()

    # Run sync and listener using asyncio.run to avoid deprecation warning
    async def client_run():
        await sync_clock()
        try:
            await listen(args.guest_id)
        except KeyboardInterrupt:
            print("Client shutting down.")
            sys.exit(0)

    asyncio.run(client_run())

if __name__ == '__main__':
    main()
