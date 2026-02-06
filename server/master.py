#!/usr/bin/env python3
"""
Master controller for synchronized audio playback.
Runs on any host (Windows, Linux) with Python 3.11+.
Uses WebSocket to send timestamped play/stop/fade commands to clients.
Clock sync should be configured via system NTP on the host and clients.
"""
import asyncio
import json
import time
import signal
import sys
from pathlib import Path
from datetime import datetime
import websockets
from aiohttp import web
import aiohttp
import socket
from zeroconf import Zeroconf, ServiceInfo
from aiohttp import WSMsgType
import subprocess
import os
import paramiko
from mutagen.mp3 import MP3
try:
    from mutagen.wave import WAVE
except ImportError:
    WAVE = None

def format_time(timestamp):
    """Convert Unix timestamp to human-readable clock format."""
    return datetime.fromtimestamp(timestamp).strftime('%H:%M:%S.%f')[:-3]

# CONFIGURATION
def _get_local_ip():
    # Use a UDP socket to determine the outbound IP without sending data.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()

HOST = '0.0.0.0'  # bind to all interfaces for mDNS access
PORT = 8765
LEAD_TIME = 10.0       # seconds before playback (increased to ensure consistent delivery)
PLAYLIST = {          # guest_id: track_filename
    '1': 'track1.mp3',
    '2': 'track2.mp3',
    # ... up to '16'
}

# Mapping guest_id -> list of playlist items for sequential playback
# Each item is a dict: {'type': 'track', 'track': 'filename.mp3'} or {'type': 'delay', 'seconds': 5}
PLAYLISTS = {}

# Cache for track durations
track_durations = {}  # filename -> duration in seconds

def get_track_duration(filename):
    """Get the duration of a WAV audio track in seconds."""
    if filename in track_durations:
        return track_durations[filename]
    try:
        path = AUDIO_DIR / filename
        # All files are now WAV (converted on upload)
        if WAVE and filename.lower().endswith('.wav'):
            audio = WAVE(str(path))
            duration = audio.info.length
        else:
            # Fallback to ffprobe
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
                                   '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                                  capture_output=True, text=True, timeout=5)
            duration = float(result.stdout.strip())
        track_durations[filename] = duration
        return duration
    except Exception as e:
        print(f"Warning: Could not get duration for {filename}: {e}")
        return 0.0

clients = {}  # guest_id -> websocket connection
client_addrs = {}  # guest_id -> client IP address
client_paths = {}  # guest_id -> client install directory
client_offsets = {}  # guest_id -> client LOCAL_OFFSET

# Trigger clients
trigger_clients = {}  # trigger_id -> websocket connection
trigger_addrs = {}  # trigger_id -> client IP address

async def register(websocket):
    """Register new client by guest_id header"""
    # Expect first message to be registration
    msg = await websocket.recv()
    data = json.loads(msg)
    guest = data.get('guest_id')
    if guest:
        clients[guest] = websocket
        print(f"Registered guest {guest}")
    else:
        print("Client failed to register, missing guest_id")

async def unregister(websocket):
    for gid, ws in list(clients.items()):
        if ws is websocket:
            del clients[gid]
            print(f"Unregistered guest {gid}")

async def handler(websocket, path):
    await register(websocket)
    try:
        async for _ in websocket:  # ignore incoming
            pass
    finally:
        await unregister(websocket)

async def start_show():
    # compute absolute start time
    base_time = time.time() + LEAD_TIME
    print(f"Base start time: {format_time(base_time)} (+{LEAD_TIME}s)")
    # send sequential play commands per guest and trigger events for triggers
    guest_end_times = {}
    for guest_id, ws in clients.items():
        # get playlist entries
        entries = PLAYLISTS.get(guest_id) or [{'type': 'track', 'track': PLAYLIST.get(guest_id)}]
        if not entries:
            print(f"Warning: guest {guest_id} has no playlist entries")
            continue
        t = base_time
        # Calculate the actual end time of the playlist by finding max(start + duration)
        playlist_end_time = base_time
        for item in entries:
            if item.get('type') == 'track':
                duration = float(item.get('duration', 0))
                start_offset = item.get('start')
                if start_offset is not None:
                    end = base_time + float(start_offset) + duration
                else:
                    end = base_time + duration
                playlist_end_time = max(playlist_end_time, end)
            elif item.get('type') == 'delay':
                delay_seconds = float(item.get('seconds', 0))
                start_offset = item.get('start')
                if start_offset is not None:
                    end = base_time + float(start_offset) + delay_seconds
                else:
                    end = base_time + delay_seconds
                playlist_end_time = max(playlist_end_time, end)
            elif item.get('type') == 'trigger':
                # Treat trigger as instantaneous but still calculate end time
                start_offset = item.get('start')
                if start_offset is not None:
                    end = base_time + float(start_offset)
                else:
                    end = base_time
                playlist_end_time = max(playlist_end_time, end)
        # Now send all PLAY commands
        for item in entries:
            if item.get('type') == 'track':
                track = item.get('track')
                if not track:
                    continue
                # If a start offset is provided, schedule at base_time + start
                start_offset = item.get('start')
                if start_offset is not None:
                    play_time = base_time + float(start_offset)
                else:
                    play_time = t
                # compute duration and include it in the PLAY command so clients can calculate delays
                duration = get_track_duration(track)
                cmd = {
                    'type': 'PLAY',
                    'track': track,
                    'timestamp': play_time,
                    'offset': 0.0,
                    'duration': duration
                }
                try:
                    await ws.send_json(cmd)
                    print(f"[{format_time(play_time)}] Starting playback: {track} for guest {guest_id}")
                except Exception as e:
                    print(f"Failed to send PLAY {track} to guest {guest_id}: {e}")
                end_time = play_time + duration
                print(f"[{format_time(end_time)}] Playback finished: {track} (duration: {duration:.1f}s)")
                if item.get('start') is None:
                    t = end_time
            elif item.get('type') == 'trigger':
                # Send trigger events
                trigger_name = item.get('trigger')
                if not trigger_name:
                    continue
                start_offset = item.get('start')
                if start_offset is not None:
                    trigger_time = base_time + float(start_offset)
                else:
                    trigger_time = t
                cmd = {
                    'type': 'TRIGGER_EVENT',
                    'trigger': trigger_name,
                    'timestamp': trigger_time
                }
                # Send to appropriate trigger client
                if trigger_name in trigger_clients:
                    ws_trigger = trigger_clients[trigger_name]
                    try:
                        await ws_trigger.send_json(cmd)
                        print(f"[{format_time(trigger_time)}] Trigger event: {trigger_name}")
                    except Exception as e:
                        print(f"Failed to send TRIGGER_EVENT {trigger_name}: {e}")
                else:
                    print(f"Warning: trigger client {trigger_name} not connected")
                if item.get('start') is None:
                    t = trigger_time
            elif item.get('type') == 'delay':
                # delays with explicit start are informational; a delay with no start is applied to t
                delay_seconds = float(item.get('seconds', 0))
                if item.get('start') is None:
                    if delay_seconds > 0:
                        print(f"[{format_time(t)}] Delay: {delay_seconds:.1f}s before next track")
                    t += delay_seconds
                else:
                    # If explicit start exists, log the delay at that point
                    print(f"[{format_time(base_time + float(item.get('start')))}] Delay: {delay_seconds:.1f}s (explicit)")
        # At the end of this guest's playlist, schedule a playlist-complete message for the client with the correct end time
        guest_end_times[guest_id] = playlist_end_time
        try:
            await ws.send_json({'type': 'PLAYLIST_COMPLETE', 'timestamp': playlist_end_time})
            print(f"[{format_time(playlist_end_time)}] Scheduled PLAYLIST_COMPLETE for guest {guest_id}")
        except Exception as e:
            print(f"Failed to send PLAYLIST_COMPLETE to guest {guest_id}: {e}")
        # Schedule a server-side notifier to print "Playlist Complete" at runtime for this guest
        async def notify_playlist_complete(gid, end_ts):
            now = time.time()
            delay = end_ts - now
            if delay > 0:
                await asyncio.sleep(delay)
            print(f"[{format_time(end_ts)}] Playlist Complete for guest {gid}")
        asyncio.create_task(notify_playlist_complete(guest_id, playlist_end_time))
    print(f"All PLAY commands dispatched at {format_time(time.time())}")
    # Schedule a global show-complete notifier at the max end time
    if guest_end_times:
        global_end = max(guest_end_times.values())
        print(f"Show scheduled to complete at {format_time(global_end)}")
        async def notify_show_complete(end_ts):
            now = time.time()
            delay = end_ts - now
            if delay > 0:
                await asyncio.sleep(delay)
            print(f"[{format_time(end_ts)}] Show Complete")
            # send optional SHOW_COMPLETE to all clients
            for gid, ws in clients.items():
                try:
                    await ws.send_json({'type': 'SHOW_COMPLETE', 'timestamp': end_ts})
                except Exception as e:
                    print(f"Failed to send SHOW_COMPLETE to guest {gid}: {e}")
        asyncio.create_task(notify_show_complete(global_end))

def signal_handler(sig, frame):
    print("Shutting down master...")
    sys.exit(0)

# HTTP and WS server on same port
HTTP_PORT = 8080
ALIAS = 'willowcrestmanor'
local_ip = _get_local_ip()  # Hostname without .local for mDNS service

# directory for uploaded tracks
AUDIO_DIR = Path(__file__).parent / 'audio'
AUDIO_DIR.mkdir(exist_ok=True)

async def index(request):
    # list connected clients and uploaded tracks
    guests = list(clients.keys())
    tracks = [p.name for p in AUDIO_DIR.glob('*.mp3')]
    html = ('<html>'
            '<head>'
            '<meta charset="utf-8"/>'
            '<title>Willowcrest Manor Audio Master Control</title>'
            '<style>'
            '* { box-sizing: border-box; }'
            'body { font-family: "Segoe UI", system-ui, sans-serif; background: #0A1612; color: #C5C1C0; margin:0; padding:0; }'
            '.header-wrapper { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:30px; }'
            '.header-left { flex:1; }'
            '.header-right { flex-shrink:0; }'
            '.container { width:75vw; max-width:1600px; min-width:700px; margin:40px auto; background:#1A2930; padding:30px; border-radius:12px; border:1px solid #2A3F4D; }'
            'h1 { text-align:left; color:#F7C83E; font-weight:600; margin:0 0 10px 0; }'
            '.subtitle { text-align:left; color:#B5B1B0; margin:0 0 10px 0; }'
            'h2 { color:#C5C1C0; margin-top:30px; font-size:1.2em; border-bottom:1px solid #2A3F4D; padding-bottom:10px; }'
            '.guest-list { list-style:none; padding:0; margin:0; }'
            '.guest-card { background:#0F1E21; border:1px solid #2A3F4D; border-radius:8px; padding:16px; margin-bottom:16px; }'
            '.guest-header { font-size:1.1em; color:#F7C83E; margin-bottom:12px; font-weight:500; }'
            'form { margin-top:12px; }'
            'input[type=text], input[type=file] { background:#0A1612; border:1px solid #2A3F4D; color:#C5C1C0; padding:8px 12px; border-radius:6px; }'
            'input[type=file] { padding:6px; }'
            'input[type=number] { background:#0A1612; border:1px solid #2A3F4D; color:#C5C1C0; padding:4px 8px; border-radius:4px; }'
            '.btn { border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:500; font-size:0.9em; transition:all 0.2s; }'
            '.btn-primary { background:#F7C83E; color:#0A1612; }'
            '.btn-primary:hover { background:#FDD859; }'
            '.btn-secondary { background:#0F1E21; color:#C5C1C0; border:1px solid #2A3F4D; }'
            '.btn-secondary:hover { background:#1A2930; }'
            '.btn-delay { background:#F7C83E; color:#0A1612; }'
            '.btn-delay:hover { background:#FDD859; }'
            '.btn-upload { background:#F7C83E; color:#0A1612; }'
            '.btn-upload:hover { background:#FDD859; }'
            '.btn-danger { background:#D97777; color:#fff; }'
            '.btn-danger:hover { background:#E89393; }'
            '.btn-go { background:#F7C83E; color:#0A1612; font-size:1.2em; padding:12px 40px; }'
            '.btn-go:hover { background:#FDD859; }'
            '.btn-group { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px; }'
            '.track-row { background:#1A2930; border:1px solid #F7C83E !important; }'
            '.delay-row { background:#1A2930; border:1px solid #F7C83E !important; }'
            '.upload-input { display:none; }'
            '.start-section { display:none; }'
            '.start-button-wrapper { position:fixed; top:30px; right:30px; z-index:100; }'
            '.timeline-container { width: 100%; max-width: 1600px; margin: 0 auto 12px auto; display:flex; }'
            '.unified-timeline-section { margin-top:20px; }'
            '.timeline-controls { margin-bottom:20px; }'
            '.timeline-column-fixed { width:150px; flex-shrink:0; border-right:1px solid #2A3F4D; border-radius:8px 0 0 8px; overflow:hidden; }'
            '.timeline-column-scrollable { flex:1; overflow-x:auto; border:1px solid #2A3F4D; border-radius:0 8px 8px 0; }'
            '.timeline-column-scrollable::-webkit-scrollbar { height:8px; }'
            '.timeline-column-scrollable::-webkit-scrollbar-track { background:#0A1612; }'
            '.timeline-column-scrollable::-webkit-scrollbar-thumb { background:#F7C83E; border-radius:4px; }'
            '.timeline-column-scrollable::-webkit-scrollbar-thumb:hover { background:#FDD859; }'
            '.timeline-rows-container { display:flex; flex-direction:column; min-width:100%; }'
            '.timeline-row { display:flex; border-bottom:1px solid #2A3F4D; min-height:64px; }'
            '.timeline-row:last-child { border-bottom:none; }'
            '.timeline-row-label { width:150px; padding:6px; background:#0F1E21; display:flex; flex-direction:column; align-items:center; justify-content:center; font-weight:600; color:#F7C83E; font-size:11px; white-space:nowrap; flex-shrink:0; gap:4px; }'
            '.timeline-row-label.trigger { justify-content:center; }'
            '.row-label-text { text-align:center; font-size:11px; }'
            '.row-button-group { display:flex; gap:2px; }'
            '.row-upload-btn { padding:3px 6px; font-size:10px; background:#F7C83E; color:#0A1612; border:none; border-radius:3px; cursor:pointer; white-space:nowrap; font-weight:600; }'
            '.row-upload-btn:hover { background:#FDD859; }'
            '.row-delay-btn { padding:3px 6px; font-size:10px; background:#F7C83E; color:#0A1612; border:none; border-radius:3px; cursor:pointer; white-space:nowrap; font-weight:600; }'
            '.row-delay-btn:hover { background:#FDD859; }'
            '.upload-input-row { display:none; }'
            '.timeline-row-content { flex:1; position:relative; height:64px; min-height:64px; }'
            '.timeline-ruler { overflow-x:auto; overflow-y:hidden; height:40px; }'
            '.timeline-item .label { font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width: calc(100% - 60px); display:inline-block; }'
            '.timeline-item .label[title]:hover { text-decoration:underline; }'
            '.timeline-item .meta { font-size:11px; color:#A59594; margin-left:8px; }'
            '.resize-handle { position:absolute; right:6px; top:50%; transform:translateY(-50%); width:10px; height:56%; background:rgba(247,200,62,0.2); border-radius:4px; cursor:ew-resize; }'
            '.zoom-controls { position:absolute; right:12px; top:6px; display:flex; gap:6px; align-items:center; display:none; }'
            '.zoom-controls .btn { padding:6px 10px; font-size:12px; }'
            '.zoom-level { color:#B5B1B0; font-size:12px; padding:4px 8px; }'
            '.upload-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:1000; justify-content:center; align-items:center; }'
            '.upload-overlay.active { display:flex; }'
            '.upload-message { background:#1A2930; border:2px solid #F7C83E; border-radius:12px; padding:40px; text-align:center; box-shadow:0 8px 32px rgba(0,0,0,0.8); }'
            '.upload-message h2 { color:#F7C83E; margin:0 0 15px 0; font-size:1.5em; }'
            '.upload-message p { color:#C5C1C0; margin:0; font-size:1.1em; }'
            '.spinner { display:inline-block; width:40px; height:40px; border:4px solid #2A3F4D; border-top:4px solid #F7C83E; border-radius:50%; animation:spin 1s linear infinite; margin-bottom:15px; }'
            '@keyframes spin { 0% { transform:rotate(0deg); } 100% { transform:rotate(360deg); } }'
            '</style>'
            '<body>'
            '<div class="upload-overlay" id="uploadOverlay"><div class="upload-message"><div class="spinner"></div><h2>Please Wait</h2><p>Uploading and processing file...</p></div></div>'
            '<div class="start-button-wrapper"><form method="POST" action="/start"><button type="submit" class="btn btn-go">▶️ GO</button></form></div>'
            '<div class="container">')
    html += '<div class="header-wrapper">'
    html += '<div class="header-left"><h1>🎭 Willowcrest Manor</h1><p class="subtitle">Audio Master Control</p></div>'
    html += '</div>'
    html += '<h2>Unified Timeline</h2>'
    
    # Build combined playlist data for all guests and triggers
    all_playlists = {}
    for guest in guests:
        plist = PLAYLISTS.get(guest, [])
        enriched = []
        for item in plist:
            if item.get('type') == 'track':
                d = get_track_duration(item.get('track'))
                enriched.append({'type': 'track', 'track': item.get('track'), 'duration': d})
            elif item.get('type') == 'delay':
                enriched.append({'type': 'delay', 'seconds': item.get('seconds', 0), 'duration': item.get('seconds', 0)})
            elif item.get('type') == 'trigger':
                enriched.append({'type': 'trigger', 'trigger': item.get('trigger', ''), 'duration': 0.5})
        all_playlists[guest] = enriched
    
    # Serialize playlists for JavaScript
    playlists_json = json.dumps(all_playlists)
    
    # Get list of available triggers
    available_triggers = list(trigger_clients.keys())
    triggers_json = json.dumps(available_triggers)
    
    # Create single shared timeline container
    html += '<div class="unified-timeline-section">'
    html += '<form id="unified-playlist-form" method="POST" action="/playlist">'
    html += f'<textarea name="playlists_data" style="display:none;" id="playlists_data">{playlists_json}</textarea>'
    html += '<div class="timeline-controls">'
    html += '<div class="btn-group">'
    html += '<button type="submit" class="btn btn-primary">💾 Save All Playlists</button>'
    html += '<div class="trigger-control" style="display:flex; gap:8px; align-items:center;">'
    html += f'<select id="trigger-select" style="background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:8px 12px; border-radius:6px;"><option value="">Select Trigger...</option>'
    for trigger_id in available_triggers:
        html += f'<option value="{trigger_id}">⚡ {trigger_id}</option>'
    html += '</select>'
    html += '<button type="button" class="btn" style="background:#d946ef;color:#fff;" id="add-trigger-btn">Add Trigger</button>'
    html += '</div>'
    html += '</div>'
    html += '</div>'
    html += '<div id="timeline-container-shared" style="margin-top:20px;"></div>'
    html += '</form>'
    html += '</div>'
    
    # Pass guest list to JavaScript
    html += f'<script>var GUESTS_LIST = {json.dumps(guests)}; var TRIGGERS_LIST = {triggers_json};</script>'

    html += r'''</div>
<script>
  // Timeline with lanes, ruler, and draggable/positionable blocks
  function timeToPx(t, pps) { return Math.round(t * pps); }
  function pxToTime(px, pps) { return px / pps; }

  function makeRuler(totalSeconds, pps) {
    const ruler = document.createElement('div');
    ruler.className = 'timeline-ruler';
    ruler.style.cssText = 'position:relative; height:28px; border-bottom:1px solid #30363d; margin-bottom:8px;';
    const numTicks = Math.max(5, Math.ceil(totalSeconds / 5));
    const step = Math.max(1, Math.ceil(totalSeconds / numTicks));
    for (let t = 0; t <= totalSeconds + 0.0001; t += step) {
      const x = timeToPx(t, pps);
      const tick = document.createElement('div');
      tick.style.cssText = 'position:absolute; left:' + x + 'px; top:0; height:100%;';
      tick.innerHTML = '<div style="position:absolute; top:2px; left:0; color:#8b949e; font-size:12px;">' + new Date(t * 1000).toISOString().substr(14, 5) + '</div>' +
                       '<div style="position:absolute; top:20px; left:0; width:1px; height:8px; background:#30363d;"></div>';
      ruler.appendChild(tick);
    }
    return ruler;
  }

  function createBlock(item) {
    const div = document.createElement('div');
    div.className = 'timeline-item';
    div.dataset.type = item.type;
    if (item.type === 'track') {
      div.classList.add('track-item');
      div.dataset.track = item.track;
      div.dataset.duration = item.duration || 0;
      const name = (item.track || '').replace(/\.[^/.]+$/, '');
      const safeFull = (item.track || '').replace(/"/g, '&quot;');
      div.innerHTML = '<div class="label" title="' + safeFull + '">🎵 ' + name + '</div><div class="meta">' + (item.duration ? (item.duration.toFixed(1) + 's') : '') + '</div>';
    } else if (item.type === 'trigger') {
      div.classList.add('trigger-item');
      div.dataset.trigger = item.trigger || '';
      div.dataset.duration = 0.5;
      div.innerHTML = '<div class="label">⚡ ' + (item.trigger || 'Trigger') + '</div><div class="meta">Event</div>';
    } else {
      div.classList.add('delay-item');
      div.dataset.seconds = item.seconds || 0;
      div.dataset.duration = item.seconds || 0;
      div.innerHTML = '<div class="label">⏱️ Delay</div><div class="meta">' + (item.seconds ? (item.seconds.toFixed(1) + 's') : '') + '</div><div class="resize-handle" title="Resize delay"></div>';
    }
    if (item.start !== undefined) div.dataset.start = item.start;
    return div;
  }

  // Shared timeline for all guests and triggers
  document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('unified-playlist-form');
    const dataTextarea = document.getElementById('playlists_data');
    let allPlaylists = {};
    try { allPlaylists = JSON.parse(dataTextarea.value) || {}; } catch(e) { allPlaylists = {}; }
    
    // Compute total timeline duration across all playlists
    let totalSeconds = 0;
    Object.keys(allPlaylists).forEach(function(guestId) {
      const playlist = allPlaylists[guestId];
      playlist.forEach(function(it) {
        const dur = parseFloat(it.duration || it.seconds || 0) || 0;
        const start = (it.start !== undefined) ? parseFloat(it.start) : 0;
        totalSeconds = Math.max(totalSeconds, start + dur);
      });
    });
    totalSeconds = Math.max(totalSeconds, 30);
    
    // Choose zoom level
    const availableWidth = Math.max(window.innerWidth * 0.85, 900);
    let pps = Math.max(4, Math.min(200, Math.round(availableWidth / totalSeconds)));
    
    // Create timeline container with fixed and scrollable columns
    const timelineContainer = document.getElementById('timeline-container-shared');
    timelineContainer.style.display = 'flex';
    timelineContainer.style.gap = '0';
    
    // Create fixed column for labels and buttons
    const rowsContainerFixed = document.createElement('div');
    rowsContainerFixed.className = 'timeline-column-fixed';
    
    // Create scrollable column for timeline
    const rowsContainerScrollable = document.createElement('div');
    rowsContainerScrollable.className = 'timeline-column-scrollable';
    
    // Create ruler inside scrollable column
    let ruler = makeRuler(Math.ceil(totalSeconds+1), pps);
    const totalPx = Math.max(Math.round(totalSeconds * pps), Math.round(availableWidth));
    ruler.style.width = (totalPx + 120) + 'px';
    rowsContainerScrollable.appendChild(ruler);
    
    // Create matching spacer in fixed column for alignment (28px to match ruler height)
    const fixedSpacer = document.createElement('div');
    fixedSpacer.style.cssText = 'height:28px; border-bottom:1px solid #30363d; background:#1c1f24;';
    rowsContainerFixed.appendChild(fixedSpacer);
    
    const rowsContainer = document.createElement('div');
    rowsContainer.className = 'timeline-rows-container';
    
    // Create rows for each guest
    const rowDivs = {};
    GUESTS_LIST.forEach(function(guestId) {
      // Fixed column row (label + buttons)
      const fixedRow = document.createElement('div');
      fixedRow.className = 'timeline-row';
      fixedRow.style.borderRight = '1px solid #30363d';
      
      const labelDiv = document.createElement('div');
      labelDiv.className = 'timeline-row-label';
      const labelText = document.createElement('div');
      labelText.className = 'row-label-text';
      labelText.textContent = '👤 Guest ' + guestId;
      labelDiv.appendChild(labelText);
      
      const buttonGroup = document.createElement('div');
      buttonGroup.className = 'row-button-group';
      
      const uploadLabel = document.createElement('label');
      uploadLabel.className = 'row-upload-btn';
      uploadLabel.textContent = '📁';
      uploadLabel.title = 'Add MP3 file(s) for this guest';
      const uploadInput = document.createElement('input');
      uploadInput.type = 'file';
      uploadInput.className = 'upload-input-row';
      uploadInput.accept = '.mp3';
      uploadInput.multiple = true;
      uploadInput.dataset.guest = guestId;
      uploadLabel.appendChild(uploadInput);
      buttonGroup.appendChild(uploadLabel);
      
      const delayBtn = document.createElement('button');
      delayBtn.className = 'row-delay-btn';
      delayBtn.textContent = '⏱️';
      delayBtn.type = 'button';
      delayBtn.title = 'Add delay to this guest\'s timeline';
      delayBtn.dataset.guest = guestId;
      buttonGroup.appendChild(delayBtn);
      
      labelDiv.appendChild(buttonGroup);
      fixedRow.appendChild(labelDiv);
      rowsContainerFixed.appendChild(fixedRow);
      
      // Scrollable column row (timeline content)
      const scrollRow = document.createElement('div');
      scrollRow.className = 'timeline-row';
      const contentDiv = document.createElement('div');
      contentDiv.className = 'timeline-row-content';
      contentDiv.style.minWidth = (Math.max(400, Math.round(totalSeconds * pps)) + 60) + 'px';
      contentDiv.dataset.rowType = 'guest';
      contentDiv.dataset.rowId = guestId;
      scrollRow.appendChild(contentDiv);
      rowsContainer.appendChild(scrollRow);
      rowDivs[guestId] = contentDiv;
      
      // Wire up delay button
      delayBtn.addEventListener('click', function() {
        const block = createBlock({ type: 'delay', seconds: 3.0, duration: 3.0 });
        const last = Array.from(rowDivs[guestId].children).reduce((m, b) => Math.max(m, parseFloat(b.dataset.start || 0) + parseFloat(b.dataset.duration || 0)), 0);
        placeBlock(block, last || 0, rowDivs[guestId]);
      });
    });
    
    // Create row for triggers
    const triggerFixedRow = document.createElement('div');
    triggerFixedRow.className = 'timeline-row';
    triggerFixedRow.style.borderRight = '1px solid #30363d';
    const triggerLabelDiv = document.createElement('div');
    triggerLabelDiv.className = 'timeline-row-label trigger';
    triggerLabelDiv.textContent = '⚡ Triggers';
    triggerFixedRow.appendChild(triggerLabelDiv);
    rowsContainerFixed.appendChild(triggerFixedRow);
    
    const triggerScrollRow = document.createElement('div');
    triggerScrollRow.className = 'timeline-row';
    const triggerContent = document.createElement('div');
    triggerContent.className = 'timeline-row-content';
    triggerContent.style.minWidth = (Math.max(400, Math.round(totalSeconds * pps)) + 60) + 'px';
    triggerContent.dataset.rowType = 'trigger';
    triggerContent.dataset.rowId = 'triggers';
    triggerScrollRow.appendChild(triggerContent);
    rowsContainer.appendChild(triggerScrollRow);
    rowDivs['triggers'] = triggerContent;
    
    rowsContainerScrollable.appendChild(rowsContainer);
    timelineContainer.appendChild(rowsContainerFixed);
    timelineContainer.appendChild(rowsContainerScrollable);
    
    // Add zoom controls
    const controlsDiv = document.querySelector('.timeline-controls .btn-group');
    const initialZoomMultiplier = (pps / 36).toFixed(2);
    const zoomControls = document.createElement('div');
    zoomControls.className = 'zoom-controls';
    zoomControls.style.cssText = 'position:static; gap:8px; margin-left:auto; display:flex; align-items:center;';
    zoomControls.innerHTML = '<button type="button" class="btn btn-secondary zoom-out">−</button><div class="zoom-level">Zoom: ' + initialZoomMultiplier + 'x</div><button type="button" class="btn btn-secondary zoom-in">+</button>';
    controlsDiv.appendChild(zoomControls);
    
    // Place blocks in rows
    function placeBlock(block, startSec, rowContent) {
      const left = timeToPx(startSec, pps);
      block.style.position = 'absolute';
      block.style.left = left + 'px';
      block.style.top = '8px';
      block.style.height = '48px';
      block.style.lineHeight = '1.1';
      block.style.padding = '6px 8px';
      block.style.borderRadius = '6px';
      block.style.display = 'flex';
      block.style.alignItems = 'center';
      block.style.justifyContent = 'space-between';
      block.style.gap = '8px';
      block.style.boxSizing = 'border-box';
      block.style.color = '#c9d1d9';
      block.style.cursor = 'grab';
      const dur = parseFloat(block.dataset.duration || 0);
      const width = Math.max(40, Math.round(dur * pps));
      block.style.width = width + 'px';
      
      // Color by type
      if (block.dataset.type === 'track') {
        block.style.background = '#0b4c2e';
        block.style.border = '2px solid #238636';
      } else if (block.dataset.type === 'trigger') {
        block.style.background = '#3d1c2e';
        block.style.border = '2px solid #d946ef';
      } else {
        block.style.background = '#3b2f0b';
        block.style.border = '2px solid #9e6a03';
      }
      
      block.dataset.start = startSec;
      rowContent.appendChild(block);
      
      // Add drag handler
      let isDragging = false;
      let dragStartX = 0;
      let dragStartLeft = 0;
      block.addEventListener('mousedown', function(e) {
        if (block.dataset.type === 'delay' && e.target.classList.contains('resize-handle')) {
          // Resize mode
          isDragging = 'resize';
          dragStartX = e.clientX;
          dragStartLeft = parseFloat(block.style.width);
        } else {
          // Drag mode
          isDragging = 'drag';
          dragStartX = e.clientX;
          dragStartLeft = parseFloat(block.style.left);
          block.style.cursor = 'grabbing';
        }
        e.preventDefault();
      });
      
      document.addEventListener('mousemove', function(e) {
        if (!isDragging) return;
        const deltaX = e.clientX - dragStartX;
        if (isDragging === 'drag') {
          let newLeft = Math.max(0, dragStartLeft + deltaX);
          
          // Snap to nearby blocks
          const snapDistance = 8; // pixels
          const otherBlocks = Array.from(rowContent.querySelectorAll('.timeline-item')).filter(b => b !== block);
          let snappedLeft = newLeft;
          let minDistance = snapDistance;
          
          otherBlocks.forEach(function(otherBlock) {
            const otherLeft = parseFloat(otherBlock.style.left);
            const otherRight = otherLeft + parseFloat(otherBlock.style.width);
            const blockWidth = parseFloat(block.style.width);
            
            // Snap to left edge of other block
            const distToOtherLeft = Math.abs(newLeft - otherLeft);
            if (distToOtherLeft < minDistance) {
              minDistance = distToOtherLeft;
              snappedLeft = otherLeft;
            }
            
            // Snap to right edge of other block
            const distToOtherRight = Math.abs(newLeft - otherRight);
            if (distToOtherRight < minDistance) {
              minDistance = distToOtherRight;
              snappedLeft = otherRight;
            }
            
            // Snap right edge to other block's left
            const distRightToLeft = Math.abs((newLeft + blockWidth) - otherLeft);
            if (distRightToLeft < minDistance) {
              minDistance = distRightToLeft;
              snappedLeft = otherLeft - blockWidth;
            }
            
            // Snap right edge to other block's right
            const distRightToRight = Math.abs((newLeft + blockWidth) - otherRight);
            if (distRightToRight < minDistance) {
              minDistance = distRightToRight;
              snappedLeft = otherRight - blockWidth;
            }
          });
          
          newLeft = Math.max(0, snappedLeft);
          block.style.left = newLeft + 'px';
          block.dataset.start = pxToTime(newLeft, pps);
        } else if (isDragging === 'resize') {
          const newWidth = Math.max(40, dragStartLeft + deltaX);
          block.style.width = newWidth + 'px';
          block.dataset.duration = pxToTime(newWidth, pps);
          block.dataset.seconds = pxToTime(newWidth, pps);
          const meta = block.querySelector('.meta');
          if (meta) meta.textContent = (pxToTime(newWidth, pps)).toFixed(1) + 's';
        }
      });
      
      document.addEventListener('mouseup', function() {
        if (isDragging === 'drag') {
          block.style.cursor = 'grab';
        }
        isDragging = false;
      });
    }
    
    // Place all blocks
    Object.keys(allPlaylists).forEach(function(guestId) {
      const playlist = allPlaylists[guestId];
      const rowContent = rowDivs[guestId];
      let cursor = 0;
      playlist.forEach(function(it) {
        const block = createBlock(it);
        let start = (it.start !== undefined) ? parseFloat(it.start) : null;
        if (start === null) { start = cursor; }
        placeBlock(block, start, rowContent);
        cursor = Math.max(cursor, start + parseFloat(it.duration || it.seconds || 0));
      });
    });
    
    // Add trigger button handler
    document.getElementById('add-trigger-btn').addEventListener('click', function() {
      const select = document.getElementById('trigger-select');
      const triggerId = select.value;
      if (!triggerId) { alert('Please select a trigger'); return; }
      
      const block = createBlock({ type: 'trigger', trigger: triggerId, duration: 0.5 });
      const last = Array.from(rowDivs['triggers'].children).reduce((m, b) => Math.max(m, parseFloat(b.dataset.start || 0) + parseFloat(b.dataset.duration || 0)), 0);
      placeBlock(block, last || 0, rowDivs['triggers']);
      select.value = '';
    });
    
    // Add per-guest upload handlers
    document.querySelectorAll('.upload-input-row').forEach(function(input) {
      input.addEventListener('change', function() {
        const guestId = this.dataset.guest;
        if (this.files.length === 0) return;
        
        const formData = new FormData();
        formData.append('guest_id', guestId);
        for (let i = 0; i < this.files.length; i++) {
          formData.append('file', this.files[i]);
        }
        
        // Show upload overlay
        const overlay = document.getElementById('uploadOverlay');
        if (overlay) {
          // Reset overlay to initial state
          overlay.querySelector('.upload-message').innerHTML = '<div class="spinner"></div><h2>Please Wait</h2><p>Uploading and processing file...</p>';
          overlay.classList.add('active');
        }
        
        fetch('/upload', {
          method: 'POST',
          body: formData
        }).then(response => response.json()).then(data => {
          if (data.files) {
            // Add uploaded files to the guest timeline
            data.files.forEach(function(file) {
              const block = createBlock({ type: 'track', track: file.filename, duration: file.duration || 0 });
              const last = Array.from(rowDivs[guestId].children).reduce((m, b) => Math.max(m, parseFloat(b.dataset.start || 0) + parseFloat(b.dataset.duration || 0)), 0);
              placeBlock(block, last || 0, rowDivs[guestId]);
            });
            // Show success message in overlay
            if (overlay) {
              const msgDiv = overlay.querySelector('.upload-message');
              if (msgDiv) {
                msgDiv.innerHTML = '<h2 style="color:#238636;">✓ Success!</h2><p>Files uploaded and added to timeline</p>';
                setTimeout(() => {
                  overlay.classList.remove('active');
                }, 2000);
              }
            }
          }
        }).catch(err => {
          if (overlay) {
            const msgDiv = overlay.querySelector('.upload-message');
            if (msgDiv) {
              msgDiv.innerHTML = '<h2 style="color:#f85149;">✕ Upload Failed</h2><p>' + err + '</p>';
              setTimeout(() => {
                overlay.classList.remove('active');
              }, 3000);
            }
          }
        });
        
        // Reset file input
        this.value = '';
      });
    });
    
    // Update zoom controls
    function updateScale(newPps) {
      pps = Math.max(4, Math.min(400, Math.round(newPps)));
      ruler.remove();
      ruler = makeRuler(Math.ceil(totalSeconds+1), pps);
      const totalPx = Math.max(Math.round(totalSeconds * pps), Math.round(availableWidth));
      ruler.style.width = (totalPx + 120) + 'px';
      timelineContainer.insertBefore(ruler, rowsContainer);
      
      // Update all row widths
      Object.keys(rowDivs).forEach(function(key) {
        rowDivs[key].style.minWidth = (Math.max(400, Math.round(totalSeconds * pps)) + 60) + 'px';
        Array.from(rowDivs[key].querySelectorAll('.timeline-item')).forEach(function(b) {
          const start = parseFloat(b.dataset.start || 0);
          const dur = parseFloat(b.dataset.duration || 0);
          b.style.left = timeToPx(start, pps) + 'px';
          b.style.width = Math.max(40, Math.round(dur * pps)) + 'px';
        });
      });
      
      const zoomMult = (pps / 36).toFixed(2);
      zoomControls.querySelector('.zoom-level').textContent = 'Zoom: ' + zoomMult + 'x';
    }
    
    // Wire zoom buttons
    zoomControls.querySelector('.zoom-in').addEventListener('click', function() { updateScale(pps + 9); });
    zoomControls.querySelector('.zoom-out').addEventListener('click', function() { updateScale(pps - 9); });
    
    // Wheel zoom
    timelineContainer.addEventListener('wheel', function(e) {
      e.preventDefault();
      if (e.deltaY < 0) updateScale(pps + 9);
      else updateScale(pps - 9);
    }, { passive: false });
    
    // Handle form submission - collect all timeline data
    form.addEventListener('submit', function(e) {
      e.preventDefault();
      
      // Show save overlay
      const overlay = document.getElementById('uploadOverlay');
      if (overlay) {
        overlay.querySelector('.upload-message').innerHTML = '<div class="spinner"></div><h2>Please Wait</h2><p>Saving playlists...</p>';
        overlay.classList.add('active');
      }
      
      // Collect all blocks from each row
      const allPlaylistsData = {};
      Object.keys(rowDivs).forEach(function(rowId) {
        const blocks = Array.from(rowDivs[rowId].querySelectorAll('.timeline-item'));
        const items = [];
        blocks.forEach(function(block) {
          const type = block.dataset.type;
          const start = parseFloat(block.dataset.start);
          const item = { type: type, start: start };
          
          if (type === 'track') {
            item.track = block.dataset.track;
            item.duration = parseFloat(block.dataset.duration);
          } else if (type === 'trigger') {
            item.trigger = block.dataset.trigger;
          } else if (type === 'delay') {
            item.seconds = parseFloat(block.dataset.seconds);
          }
          items.push(item);
        });
        
        // Sort by start time
        items.sort((a, b) => a.start - b.start);
        allPlaylistsData[rowId] = items;
      });
      
      // Update textarea with collected data
      dataTextarea.value = JSON.stringify(allPlaylistsData);
      
      // Submit form to /playlist endpoint
      const formData = new FormData();
      formData.append('playlists_data', dataTextarea.value);
      
      fetch('/playlist-unified', {
        method: 'POST',
        body: formData
      }).then(response => {
        if (response.ok) {
          const overlay = document.getElementById('uploadOverlay');
          if (overlay) {
            overlay.querySelector('.upload-message').innerHTML = '<h2 style="color:#238636;">✓ Success!</h2><p>Playlists saved!</p>';
            setTimeout(() => {
              overlay.classList.remove('active');
            }, 2000);
          }
        } else {
          const overlay = document.getElementById('uploadOverlay');
          if (overlay) {
            overlay.querySelector('.upload-message').innerHTML = '<h2 style="color:#f85149;">✕ Error</h2><p>Failed to save playlists</p>';
            setTimeout(() => {
              overlay.classList.remove('active');
            }, 3000);
          }
        }
      }).catch(err => {
        const overlay = document.getElementById('uploadOverlay');
        if (overlay) {
          overlay.querySelector('.upload-message').innerHTML = '<h2 style="color:#f85149;">✕ Error</h2><p>' + err + '</p>';
          setTimeout(() => {
            overlay.classList.remove('active');
          }, 3000);
        }
      });
    });
  });
</script>
</body></html>'''
  
    return web.Response(text=html, content_type='text/html')

async def ws_handler(request):
    from aiohttp import WSMsgType
    # WebSocket handler: support time sync and client registration (both audio and trigger clients)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    # register: expect first TEXT message with registration, install_dir, and offset
    try:
        text = await ws.receive_str()
    except Exception:
        await ws.close()
        return ws
    data = json.loads(text)
    gid = data.get('guest_id')
    tid = data.get('trigger_id')
    
    # Register either a guest (audio) client or a trigger client
    if gid:
        clients[gid] = ws
        client_addrs[gid] = request.remote
        client_paths[gid] = data.get('install_dir', '')
        client_offsets[gid] = float(data.get('local_offset', 0.0))
        print(f'[{format_time(time.time())}] Registered guest {gid} at {request.remote} with offset={client_offsets[gid]}s')
        client_type = 'guest'
    elif tid:
        trigger_clients[tid] = ws
        trigger_addrs[tid] = request.remote
        print(f'[{format_time(time.time())}] Registered trigger client {tid} at {request.remote}')
        client_type = 'trigger'
    else:
        await ws.close()
        return ws
    
    try:
        async for msg in ws:
            # skip non-text messages
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            # Handle time sync requests from clients
            if data.get('type') == 'TIME_REQUEST':
                reply = {'type': 'TIME_REPLY', 'server_time': time.time()}
                await ws.send_json(reply)
                continue
            # Ignore other messages
            pass
    finally:
        if client_type == 'guest':
            clients.pop(gid, None)
            client_addrs.pop(gid, None)
            client_paths.pop(gid, None)
            client_offsets.pop(gid, None)
        else:
            trigger_clients.pop(tid, None)
            trigger_addrs.pop(tid, None)
    return ws

async def upload_handler(request):
    try:
        reader = await request.multipart()
        # First form field: guest ID
        field = await reader.next()
        guest_id = (await field.text()).strip()
        # Process any number of file parts (support multi-file upload)
        files_added = []
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name != 'file':
                # skip unexpected fields
                continue
            # use original filename, replace spaces with underscores
            original_fname = field.filename or 'track'
            safe_fname = original_fname.replace(' ', '_')
            # If MP3, convert to WAV; otherwise keep as is
            if safe_fname.lower().endswith('.mp3'):
                wav_fname = safe_fname.rsplit('.', 1)[0] + '.wav'
            else:
                wav_fname = safe_fname
            
            temp_path = AUDIO_DIR / safe_fname
            final_path = AUDIO_DIR / wav_fname
            
            # Save uploaded file
            with open(temp_path, 'wb') as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            
            print(f"[{format_time(time.time())}] Uploaded {safe_fname} for guest {guest_id}")
            
            # Convert MP3 to WAV if necessary
            if safe_fname.lower().endswith('.mp3'):
                try:
                    print(f"[{format_time(time.time())}] Converting {safe_fname} to WAV...")
                    # Use ffmpeg to convert MP3 to WAV
                    result = subprocess.run(['ffmpeg', '-i', str(temp_path), '-acodec', 'pcm_s16le', '-ar', '44100', 
                                           str(final_path), '-y'], capture_output=True, timeout=60)
                    if result.returncode == 0:
                        temp_path.unlink()  # Delete original MP3
                        print(f"[{format_time(time.time())}] Converted to {wav_fname}")
                    else:
                        ffmpeg_error = result.stderr.decode('utf-8', errors='ignore') if result.stderr else 'Unknown error'
                        print(f"[{format_time(time.time())}] FFmpeg error: {ffmpeg_error}")
                        print(f"[{format_time(time.time())}] Conversion failed, keeping MP3")
                        final_path = temp_path
                        wav_fname = safe_fname
                except FileNotFoundError:
                    print(f"[{format_time(time.time())}] ERROR: FFmpeg not found. Please install FFmpeg:")
                    print(f"  Windows: Download from https://ffmpeg.org/download.html or use: choco install ffmpeg")
                    print(f"  Linux: apt-get install ffmpeg")
                    print(f"  Mac: brew install ffmpeg")
                    print(f"  Keeping original MP3 file.")
                    final_path = temp_path
                    wav_fname = safe_fname
                except Exception as e:
                    print(f"[{format_time(time.time())}] Conversion error: {e}, keeping original")
                    final_path = temp_path
                    wav_fname = safe_fname
            
            # add to guest playlist (track row followed by delay row)
            PLAYLISTS.setdefault(guest_id, []).append({'type': 'track', 'track': wav_fname})
            PLAYLISTS[guest_id].append({'type': 'delay', 'seconds': 0.0})
            files_added.append({'filename': wav_fname, 'duration': get_track_duration(wav_fname)})
            
            # push file to client via SFTP if connected
            ip = client_addrs.get(guest_id)
            install_dir = client_paths.get(guest_id, '')
            if ip and install_dir:
                password = 'raspberry'
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(hostname=ip, username='pi', password=password)
                    sftp = ssh.open_sftp()
                    # Use forward slashes for remote Unix/Linux paths
                    audio_dir = install_dir.rstrip('/') + '/audio'
                    try:
                        sftp.mkdir(audio_dir)
                    except IOError:
                        pass
                    remote_path = audio_dir.rstrip('/') + '/' + wav_fname
                    sftp.put(str(final_path), remote_path)
                    sftp.close()
                    ssh.close()
                    print(f"[{format_time(time.time())}] Pushed file to client {guest_id} at {ip}:{remote_path}")
                except Exception as e:
                    print(f"Failed to push file to client {guest_id}: {e}")
        # Return JSON for AJAX requests, include durations so the UI can size the timeline
        return web.json_response({'status': 'ok', 'files': files_added})
    except Exception as e:
        print(f"[{format_time(time.time())}] Upload handler error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({'status': 'error', 'message': str(e)}, status=400)

async def start_handler(request):
    await start_show()
    return web.HTTPFound('/')

async def playlist_handler(request):
    # Handle JSON content type from JavaScript
    content_type = request.content_type
    if content_type == 'application/json':
        data = await request.json()
        guest_id = data.get('guest_id')
        playlist = data.get('playlist', [])
        # Validate and clean playlist items, allow start/duration for timeline positioning
        entries = []
        for item in playlist:
            if item.get('type') == 'track' and item.get('track'):
                entry = {'type': 'track', 'track': item['track']}
                if 'start' in item:
                    entry['start'] = float(item['start'])
                if 'duration' in item:
                    entry['duration'] = float(item['duration'])
                entries.append(entry)
            elif item.get('type') == 'delay':
                entry = {'type': 'delay', 'seconds': float(item.get('seconds', 0))}
                if 'start' in item:
                    entry['start'] = float(item['start'])
                entries.append(entry)
            elif item.get('type') == 'trigger' and item.get('trigger'):
                entry = {'type': 'trigger', 'trigger': item['trigger']}
                if 'start' in item:
                    entry['start'] = float(item['start'])
                entries.append(entry)
        PLAYLISTS[guest_id] = entries
        print(f"[{format_time(time.time())}] Updated playlist for guest {guest_id}: {entries}")
        return web.Response(text='OK', status=200)
    else:
        # Fallback for form data
        data = await request.post()
        guest_id = data.get('guest_id')
        playlist_text = data.get('playlist', '')
        entries = []
        try:
            playlist = json.loads(playlist_text)
            for item in playlist:
                if item.get('type') == 'track' and item.get('track'):
                    entry = {'type': 'track', 'track': item['track']}
                    if 'start' in item:
                        entry['start'] = float(item['start'])
                    if 'duration' in item:
                        entry['duration'] = float(item['duration'])
                    entries.append(entry)
                elif item.get('type') == 'delay':
                    entry = {'type': 'delay', 'seconds': float(item.get('seconds', 0))}
                    if 'start' in item:
                        entry['start'] = float(item['start'])
                    entries.append(entry)
                elif item.get('type') == 'trigger' and item.get('trigger'):
                    entry = {'type': 'trigger', 'trigger': item['trigger']}
                    if 'start' in item:
                        entry['start'] = float(item['start'])
                    entries.append(entry)
        except (json.JSONDecodeError, ValueError):
            pass
        PLAYLISTS[guest_id] = entries
        print(f"[{format_time(time.time())}] Updated playlist for guest {guest_id}: {entries}")
        return web.HTTPFound('/')

async def playlist_unified_handler(request):
    # Handle unified playlist submission from the shared timeline
    data = await request.post()
    playlists_text = data.get('playlists_data', '{}')
    
    try:
        all_playlists = json.loads(playlists_text)
        # Update PLAYLISTS dict for each guest
        for guest_id, items in all_playlists.items():
            entries = []
            if guest_id != 'triggers':  # Skip the triggers row, it's handled separately
                for item in items:
                    if item.get('type') == 'track' and item.get('track'):
                        entry = {'type': 'track', 'track': item['track']}
                        if 'start' in item:
                            entry['start'] = float(item['start'])
                        if 'duration' in item:
                            entry['duration'] = float(item['duration'])
                        entries.append(entry)
                    elif item.get('type') == 'delay':
                        entry = {'type': 'delay', 'seconds': float(item.get('seconds', 0))}
                        if 'start' in item:
                            entry['start'] = float(item['start'])
                        entries.append(entry)
                    elif item.get('type') == 'trigger' and item.get('trigger'):
                        entry = {'type': 'trigger', 'trigger': item['trigger']}
                        if 'start' in item:
                            entry['start'] = float(item['start'])
                        entries.append(entry)
                PLAYLISTS[guest_id] = entries
                print(f"[{format_time(time.time())}] Updated playlist for guest {guest_id}: {entries}")
        return web.json_response({'status': 'ok'})
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error parsing playlists: {e}")
        return web.json_response({'status': 'error', 'message': str(e)}, status=400)

# setup aiohttp app
app = web.Application()
# Add static route for audio files
app.router.add_static('/audio/', path=str(AUDIO_DIR), name='audio')
app.router.add_get('/', index)
app.router.add_post('/upload', upload_handler)
app.router.add_post('/start', start_handler)
app.router.add_get('/ws', ws_handler)
app.router.add_post('/playlist', playlist_handler)
app.router.add_post('/playlist-unified', playlist_unified_handler)

if __name__ == '__main__':
    # Register mDNS service for willowcrestmanor.local
    print(f'Registering mDNS service as {ALIAS}.local on {local_ip}:{HTTP_PORT}')
    zeroconf = Zeroconf()
    info = ServiceInfo(
        '_http._tcp.local.',
        f'{ALIAS}._http._tcp.local.',
        addresses=[socket.inet_aton(local_ip)],
        port=HTTP_PORT,
        properties={},
        server=f'{ALIAS}.local.'
    )
    zeroconf.register_service(info)
    try:
        print(f'Starting HTTP+WS server on port {HTTP_PORT}')
        print(f'Access web UI at: http://{ALIAS}.local:{HTTP_PORT}')
        web.run_app(app, host=HOST, port=HTTP_PORT)
    finally:
        zeroconf.unregister_service(info)
        zeroconf.close()

# disable old console main
if False:
    asyncio.run(main())
