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
    """Get the duration of an MP3 track in seconds."""
    if filename in track_durations:
        return track_durations[filename]
    try:
        path = AUDIO_DIR / filename
        audio = MP3(str(path))
        duration = audio.info.length
        track_durations[filename] = duration
        return duration
    except Exception as e:
        print(f"Warning: Could not get duration for {filename}: {e}")
        return 0.0

clients = {}  # guest_id -> websocket connection
client_addrs = {}  # guest_id -> client IP address
client_paths = {}  # guest_id -> client install directory
client_offsets = {}  # guest_id -> client LOCAL_OFFSET

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
    # send sequential play commands per guest
    for guest_id, ws in clients.items():
        # get playlist entries
        entries = PLAYLISTS.get(guest_id) or [{'type': 'track', 'track': PLAYLIST.get(guest_id)}]
        if not entries:
            print(f"Warning: guest {guest_id} has no playlist entries")
            continue
        t = base_time
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
    print(f"All PLAY commands dispatched at {format_time(time.time())}")

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
    guests = clients.keys()
    tracks = [p.name for p in AUDIO_DIR.glob('*.mp3')]
    html = ('<html>'
            '<head>'
            '<meta charset="utf-8"/>'
            '<title>Willowcrest Manor Audio Master Control</title>'
            '<style>'
            '* { box-sizing: border-box; }'
            'body { font-family: "Segoe UI", system-ui, sans-serif; background: #0d1117; color: #c9d1d9; margin:0; padding:0; }'
            '.container { max-width:900px; margin:40px auto; background:#161b22; padding:30px; border-radius:12px; border:1px solid #30363d; }'
            'h1 { text-align:center; color:#58a6ff; font-weight:600; margin-bottom:10px; }'
            '.subtitle { text-align:center; color:#8b949e; margin-bottom:30px; }'
            'h2 { color:#c9d1d9; margin-top:30px; font-size:1.2em; border-bottom:1px solid #30363d; padding-bottom:10px; }'
            '.guest-list { list-style:none; padding:0; margin:0; }'
            '.guest-card { background:#21262d; border:1px solid #30363d; border-radius:8px; padding:16px; margin-bottom:16px; }'
            '.guest-header { font-size:1.1em; color:#58a6ff; margin-bottom:12px; font-weight:500; }'
            'form { margin-top:12px; }'
            'input[type=text], input[type=file] { background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:8px 12px; border-radius:6px; }'
            'input[type=file] { padding:6px; }'
            'input[type=number] { background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:4px 8px; border-radius:4px; }'
            '.btn { border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:500; font-size:0.9em; transition:all 0.2s; }'
            '.btn-primary { background:#238636; color:#fff; }'
            '.btn-primary:hover { background:#2ea043; }'
            '.btn-secondary { background:#21262d; color:#c9d1d9; border:1px solid #30363d; }'
            '.btn-secondary:hover { background:#30363d; }'
            '.btn-delay { background:#1f6feb; color:#fff; }'
            '.btn-delay:hover { background:#388bfd; }'
            '.btn-upload { background:#8957e5; color:#fff; }'
            '.btn-upload:hover { background:#a371f7; }'
            '.btn-danger { background:#da3633; color:#fff; }'
            '.btn-danger:hover { background:#f85149; }'
            '.btn-go { background:#da3633; color:#fff; font-size:1.2em; padding:12px 40px; }'
            '.btn-go:hover { background:#f85149; }'
            '.btn-group { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px; }'
            '.track-row { background:#1c3a2e; border:1px solid #238636 !important; }'
            '.delay-row { background:#2d2a1c; border:1px solid #9e6a03 !important; }'
            '.upload-input { display:none; }'
            '.start-section { text-align:center; margin-top:40px; padding-top:20px; border-top:1px solid #30363d; }'
            '</style>'
            '</head>'
            '<body><div class="container">')
    html += '<h1>🎭 Willowcrest Manor</h1>'
    html += '<p class="subtitle">Audio Master Control</p>'
    html += '<h2>Connected Guests</h2><ul class="guest-list">'
    # Playlist editor per guest
    for guest in guests:
        # display current playlist (new format: list of {type, track/seconds})
        plist = PLAYLISTS.get(guest, [])
        # enrich playlist with durations for tracks so front-end can size timeline boxes
        enriched = []
        for item in plist:
            if item.get('type') == 'track':
                d = get_track_duration(item.get('track'))
                enriched.append({'type': 'track', 'track': item.get('track'), 'duration': d})
            elif item.get('type') == 'delay':
                enriched.append({'type': 'delay', 'seconds': item.get('seconds', 0), 'duration': item.get('seconds', 0)})
        plist_json = json.dumps(enriched)
        html += f'<li class="guest-card">'
        html += f'<div class="guest-header">👤 Guest {guest}</div>'
        html += '<form method="POST" action="/playlist" class="playlist-form">'
        html += f'<input type="hidden" name="guest_id" value="{guest}" />'
        html += f'<textarea name="playlist" style="display:none;">{plist_json}</textarea>'
        html += '<div class="btn-group">'
        html += '<button type="submit" class="btn btn-primary">💾 Save Playlist</button>'
        html += '<button type="button" class="btn btn-delay add-delay-btn">⏱️ Add Delay</button>'
        html += f'<label class="btn btn-upload">📁 Upload MP3<input type="file" class="upload-input" accept=".mp3" data-guest="{guest}" /></label>'
        html += '</div>'
        html += '</form></li>'
    if not guests:
        html += '<li class="guest-card" style="text-align:center; color:#8b949e;">No guests connected</li>'
    html += '</ul>'

    # start show button
    html += '<div class="start-section">'
    html += '<h2>Start Show</h2>'
    html += '<form method="POST" action="/start">'
    html += '<button type="submit" class="btn btn-go">▶️ GO</button>'
    html += '</form>'
    html += '</div>'

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
      div.innerHTML = '<div class="label">🎵 ' + item.track + '</div><div class="meta">' + (item.duration ? (item.duration.toFixed(1) + 's') : '') + '</div>';
    } else {
      div.classList.add('delay-item');
      div.dataset.seconds = item.seconds || 0;
      div.dataset.duration = item.seconds || 0;
      div.innerHTML = '<div class="label">⏱️ Delay</div><div class="meta">' + (item.seconds ? (item.seconds.toFixed(1) + 's') : '') + '</div>';
    }
    if (item.start !== undefined) div.dataset.start = item.start;
    return div;
  }

  // Build lanes and place blocks absolutely by start/time
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('form.playlist-form').forEach(function(form) {
      const guestId = form.querySelector('input[name="guest_id"]').value;
      const textarea = form.querySelector('textarea[name="playlist"]');
      let playlist = [];
      try { playlist = JSON.parse(textarea.value) || []; } catch(e) { playlist = []; }

      // compute total seconds = max of (start + duration) or fallback 60s
      let totalSeconds = 0;
      playlist.forEach(function(it, i) {
        const dur = parseFloat(it.duration || it.seconds || 0) || 0;
        const start = (it.start !== undefined) ? parseFloat(it.start) : 0;
        totalSeconds = Math.max(totalSeconds, start + dur);
      });
      totalSeconds = Math.max(totalSeconds, 30);

      const pps = Math.max(8, Math.min(80, Math.round((Math.max(400, 600) / totalSeconds))));

      const timelineContainer = document.createElement('div');
      timelineContainer.className = 'timeline-container';
      timelineContainer.style.cssText = 'background: linear-gradient(90deg, rgba(255,255,255,0.01), rgba(255,255,255,0.01)); padding:8px; border-radius:6px;';

      // ruler
      const ruler = makeRuler(Math.ceil(totalSeconds+1), pps);
      ruler.style.width = (Math.max(400, Math.round(totalSeconds * pps)) + 60) + 'px';
      timelineContainer.appendChild(ruler);

      // single-row timeline (left-to-right) that snaps blocks together
      const lane = document.createElement('div');
      lane.className = 'timeline-lane single';
      lane.style.cssText = 'position:relative; height:64px; margin-top:6px; margin-bottom:6px; min-width:' + (Math.max(400, Math.round(totalSeconds * pps)) + 60) + 'px;';
      timelineContainer.appendChild(lane);

      // helper to place a block at a start time into the single lane
      function placeBlock(block, startSec) {
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
        // color by type
        if (block.dataset.type === 'track') {
          block.style.background = '#0b4c2e';
          block.style.border = '2px solid #238636';
        } else {
          block.style.background = '#3b2f0b';
          block.style.border = '2px solid #9e6a03';
        }
        // store start
        block.dataset.start = startSec;
        lane.appendChild(block);
      }

      // initial placement: if start exists use it, else stack items end-to-end left-to-right
      let cursor = 0;
      playlist.forEach(function(it) {
        const block = createBlock(it);
        let start = (it.start !== undefined) ? parseFloat(it.start) : null;
        if (start === null) {
          start = cursor;
        }
        placeBlock(block, start);
        cursor = Math.max(cursor, start + parseFloat(it.duration || it.seconds || 0));
      });

      // helper: reflow all blocks so they sit adjacent left-to-right with no gaps, in order of current left positions
      function reflow() {
        const blocks = Array.from(lane.querySelectorAll('.timeline-item'));
        blocks.sort((a,b)=> parseInt(a.style.left||0) - parseInt(b.style.left||0));
        let x = 0;
        blocks.forEach(b=>{
          const dur = parseFloat(b.dataset.duration||0);
          b.dataset.start = x;
          b.style.left = timeToPx(x, pps) + 'px';
          x += dur;
        });
      }

      // attach timeline to form
      const btnGroup = form.querySelector('.btn-group');
      form.insertBefore(timelineContainer, btnGroup);

      // dragging (pointer events) for moving horizontally in single row
      let dragging = null;
      function onPointerDown(e) {
        const block = e.target.closest('.timeline-item');
        if (!block) return;
        dragging = { block: block, startX: e.clientX, origLeft: parseInt(block.style.left||0) };
        if (e.pointerId) block.setPointerCapture(e.pointerId);
        block.style.cursor = 'grabbing';
        block.style.transition = 'none';
      }
      function onPointerMove(e) {
        if (!dragging) return;
        const dx = e.clientX - dragging.startX;
        const newLeft = Math.max(0, dragging.origLeft + dx);
        dragging.block.style.left = newLeft + 'px';
      }
      function onPointerUp(e) {
        if (!dragging) return;
        const block = dragging.block;
        // snap to grid time resolution (0.1s)
        const leftPx = parseInt(block.style.left||0);
        const time = Math.round(pxToTime(leftPx, pps) * 10) / 10.0;
        // Temporarily set left for sorting then reflow so blocks snap adjacent
        block.dataset.start = time;
        block.style.left = timeToPx(time, pps) + 'px';
        block.style.cursor = 'grab';
        if (e.pointerId) block.releasePointerCapture(e.pointerId);
        block.style.transition = '';
        dragging = null;
        // Now reflow all blocks so they sit adjacent
        reflow();
      }

      timelineContainer.addEventListener('pointerdown', function(e) { onPointerDown(e); });
      window.addEventListener('pointermove', function(e) { onPointerMove(e); });
      window.addEventListener('pointerup', function(e) { onPointerUp(e); });

      // double-click a delay to edit seconds (and reflow following blocks)
      timelineContainer.addEventListener('dblclick', function(e) {
        const block = e.target.closest('.timeline-item');
        if (!block || block.dataset.type !== 'delay') return;
        const current = parseFloat(block.dataset.seconds || 0);
        const val = prompt('Set delay seconds:', current);
        if (val === null) return;
        const secs = parseFloat(val) || 0;
        block.dataset.seconds = secs;
        block.dataset.duration = secs;
        block.querySelector('.meta').textContent = secs.toFixed(1) + 's';
        // update width
        const width = Math.max(40, Math.round(secs * pps));
        block.style.width = width + 'px';
        // reflow so following blocks stay adjacent
        reflow();
      });

      // right-click to remove
      timelineContainer.addEventListener('contextmenu', function(e) {
        const block = e.target.closest('.timeline-item');
        if (!block) return;
        e.preventDefault();
        if (confirm('Remove this item?')) block.remove();
      });

      // add delay button places delay at end of shortest lane
      form.querySelector('.add-delay-btn').addEventListener('click', function() {
        // find lane with smallest end
        let mins = Infinity, idx = 0;
        for (let i=0;i<lanesCount;i++) {
          const last = Array.from(lanes[i].children).reduce((m,b)=>Math.max(m, parseFloat(b.dataset.start||0) + parseFloat(b.dataset.duration||0)), 0);
          if (last < mins) { mins = last; idx = i; }
        }
        const block = createBlock({type:'delay', seconds:1.0});
        placeBlock(idx, block, mins || 0);
      });

      // file upload handler adds block to lane 0 end
      const fileInput = form.querySelector('.upload-input');
      fileInput.addEventListener('change', function() {
        if (!this.files.length) return;
        const file = this.files[0];
        const formData = new FormData();
        formData.append('guest_id', guestId);
        formData.append('file', file);
        fetch('/upload', { method: 'POST', body: formData, headers: { 'Accept': 'application/json' } })
        .then(resp => {
          if (!resp.ok) throw new Error('Upload failed (HTTP ' + resp.status + ')');
          const ctype = (resp.headers.get('Content-Type') || '');
          if (ctype.indexOf('application/json') >= 0) return resp.json();
          return resp.text().then(t => { throw new Error('Unexpected non-JSON response'); });
        })
        .then(data => {
          if (data && data.status === 'ok') {
            // place at end of lane 0
            const last = Array.from(lanes[0].children).reduce((m,b)=>Math.max(m, parseFloat(b.dataset.start||0) + parseFloat(b.dataset.duration||0)), 0);
            const block = createBlock({type:'track', track:data.filename, duration:data.duration || 0});
            placeBlock(0, block, last || 0);
            // auto-save updated timeline to server so server playlist reflects the uploaded track immediately
            const items = [];
            for (let i=0;i<lanes.length;i++) {
              Array.from(lanes[i].children).forEach(function(b) {
                const start = parseFloat(b.dataset.start || 0);
                if (b.dataset.type === 'track') items.push({ type:'track', track:b.dataset.track, start:start, duration: parseFloat(b.dataset.duration||0) });
                else items.push({ type:'delay', start:start, seconds: parseFloat(b.dataset.seconds||0) });
              });
            }
            items.sort((a,b)=> (a.start||0) - (b.start||0));
            fetch('/playlist', { method: 'POST', headers:{ 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: JSON.stringify({ guest_id: guestId, playlist: items }) })
            .then(r=> { if (!r.ok) console.warn('Failed to auto-save playlist after upload'); })
            .catch(e=> console.warn('Playlist auto-save error', e));
          } else alert('Upload failed');
        })
        .catch((err) => alert('Upload error: ' + (err && err.message ? err.message : 'Network error')))
        .finally(() => { this.value = ''; });
      });

      // Save handler: gather all blocks in the single lane, convert to playlist items with start times
      form.addEventListener('submit', function(e) {
        e.preventDefault();
        const items = [];
        Array.from(lane.children).forEach(function(block) {
          const start = parseFloat(block.dataset.start || 0);
          if (block.dataset.type === 'track') items.push({ type:'track', track:block.dataset.track, start:start, duration: parseFloat(block.dataset.duration||0) });
          else items.push({ type:'delay', start:start, seconds: parseFloat(block.dataset.seconds||0) });
        });
        // sort by start time for server convenience
        items.sort((a,b)=> (a.start||0) - (b.start||0));
        fetch('/playlist', { method:'POST', headers:{'Content-Type':'application/json', 'Accept':'application/json'}, body: JSON.stringify({ guest_id: guestId, playlist: items }) })
        .then(resp => resp.ok ? alert('Playlist saved') : alert('Error saving playlist'))
        .catch(() => alert('Network error'));
      });

      // set container width based on totalSeconds
      timelineContainer.style.width = (Math.max(400, Math.round(totalSeconds * pps)) + 80) + 'px';
    });
  });
</script>
</body></html>'''
  
    return web.Response(text=html, content_type='text/html')

async def ws_handler(request):
    from aiohttp import WSMsgType
    # WebSocket handler: support time sync and client registration
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
    if gid:
        clients[gid] = ws
        client_addrs[gid] = request.remote
        client_paths[gid] = data.get('install_dir', '')
        client_offsets[gid] = float(data.get('local_offset', 0.0))
        print(f'[{format_time(time.time())}] Registered guest {gid} at {request.remote} with offset={client_offsets[gid]}s')
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
        clients.pop(gid, None)
        client_addrs.pop(gid, None)
        client_paths.pop(gid, None)
        client_offsets.pop(gid, None)
    return ws

async def upload_handler(request):
    reader = await request.multipart()
    # First form field: guest ID
    field = await reader.next()
    guest_id = (await field.text()).strip()
    # Next form field: file
    field = await reader.next()
    if field.name == 'file':
        # use original filename, replace spaces with underscores
        original_fname = field.filename or 'track'
        safe_fname = original_fname.replace(' ', '_')
        path = AUDIO_DIR / safe_fname
        # save file locally
        with open(path, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        print(f"[{format_time(time.time())}] Uploaded {safe_fname} for guest {guest_id}")
        # add to guest playlist (track row followed by delay row)
        PLAYLISTS.setdefault(guest_id, []).append({'type': 'track', 'track': safe_fname})
        PLAYLISTS[guest_id].append({'type': 'delay', 'seconds': 0.0})
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
                audio_dir = os.path.join(install_dir, 'audio')
                try:
                    sftp.mkdir(audio_dir)
                except IOError:
                    pass
                remote_path = os.path.join(audio_dir, safe_fname)
                sftp.put(str(path), remote_path)
                sftp.close()
                ssh.close()
                print(f"[{format_time(time.time())}] Pushed file to client {guest_id} at {ip}:{remote_path}")
            except Exception as e:
                print(f"Failed to push file to client {guest_id}: {e}")
        # Return JSON for AJAX requests, include duration so the UI can size the timeline
        sec_fetch = request.headers.get('Sec-Fetch-Mode')
        xreq = request.headers.get('X-Requested-With', '')
        accept = request.headers.get('Accept', '')
        if 'application/json' in accept or xreq == 'XMLHttpRequest' or sec_fetch is not None:
            duration = get_track_duration(safe_fname)
            return web.json_response({'status': 'ok', 'filename': safe_fname, 'duration': duration})
    return web.HTTPFound('/')

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
        except (json.JSONDecodeError, ValueError):
            pass
        PLAYLISTS[guest_id] = entries
        print(f"[{format_time(time.time())}] Updated playlist for guest {guest_id}: {entries}")
        return web.HTTPFound('/')

# setup aiohttp app
app = web.Application()
# Add static route for audio files
app.router.add_static('/audio/', path=str(AUDIO_DIR), name='audio')
app.router.add_get('/', index)
app.router.add_post('/upload', upload_handler)
app.router.add_post('/start', start_handler)
app.router.add_get('/ws', ws_handler)
app.router.add_post('/playlist', playlist_handler)

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
