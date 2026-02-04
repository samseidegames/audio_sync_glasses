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
        last_track = None
        for item in entries:
            if item.get('type') == 'track':
                track = item.get('track')
                if not track:
                    continue
                cmd = {
                    'type': 'PLAY',
                    'track': track,
                    'timestamp': t,
                    'offset': 0.0
                }
                try:
                    await ws.send_json(cmd)
                    print(f"[{format_time(t)}] Starting playback: {track} for guest {guest_id}")
                except Exception as e:
                    print(f"Failed to send PLAY {track} to guest {guest_id}: {e}")
                # Add track duration to time so next track starts after this one finishes
                duration = get_track_duration(track)
                end_time = t + duration
                print(f"[{format_time(end_time)}] Playback finished: {track} (duration: {duration:.1f}s)")
                t = end_time
                last_track = track
            elif item.get('type') == 'delay':
                # add delay to the current time (after previous track has finished)
                delay_seconds = float(item.get('seconds', 0))
                if delay_seconds > 0:
                    print(f"[{format_time(t)}] Delay: {delay_seconds:.1f}s before next track")
                t += delay_seconds
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
  // Timeline rendering utilities
  function pxPerSecondFor(container, totalSeconds) {
    const maxWidth = Math.max(400, container.clientWidth - 40);
    if (totalSeconds <= 0) return 40;
    let pps = maxWidth / totalSeconds; // fit whole timeline
    // clamp to reasonable bounds
    pps = Math.max(8, Math.min(pps, 80));
    return pps;
  }

  function createBlock(item) {
    const div = document.createElement('div');
    div.className = (item.type === 'track') ? 'timeline-item track-item' : 'timeline-item delay-item';
    div.setAttribute('draggable', 'true');
    div.dataset.type = item.type;
    if (item.type === 'track') {
      div.dataset.track = item.track;
      div.dataset.duration = item.duration || 0;
      div.innerHTML = '<div class="label">🎵 ' + item.track + '</div><div class="meta">' + (item.duration ? (item.duration.toFixed(1) + 's') : '') + '</div>';
    } else {
      div.dataset.seconds = item.seconds || 0;
      div.dataset.duration = item.seconds || 0;
      div.innerHTML = '<div class="label">⏱️ Delay</div><div class="meta">' + (item.seconds ? (item.seconds.toFixed(1) + 's') : '') + '</div>';
    }
    return div;
  }

  function renderTimeline(ul) {
    // ul is the timeline container
    const items = Array.from(ul.children);
    // compute total seconds
    let total = 0;
    items.forEach(function(div) { total += parseFloat(div.dataset.duration || 0); });
    const pps = pxPerSecondFor(ul, total);
    items.forEach(function(div) {
      const dur = parseFloat(div.dataset.duration || 0);
      const width = Math.max(24, Math.round(dur * pps));
      div.style.width = width + 'px';
    });
  }

  // Replace playlist-list with horizontal timeline
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('form.playlist-form').forEach(function(form) {
      const guestId = form.querySelector('input[name="guest_id"]').value;
      const textarea = form.querySelector('textarea[name="playlist"]');
      let playlist = [];
      try { playlist = JSON.parse(textarea.value) || []; } catch(e) { playlist = []; }

      const timeline = document.createElement('div');
      timeline.className = 'timeline';
      timeline.style.cssText = 'display:flex; align-items:center; gap:8px; padding:8px; min-height:64px; overflow-x:auto; border-radius:8px;';

      // populate
      playlist.forEach(function(item) {
        const block = createBlock(item);
        timeline.appendChild(block);
      });

      // insert timeline before btn-group
      const btnGroup = form.querySelector('.btn-group');
      form.insertBefore(timeline, btnGroup);

      // make timeline draggable for reorder
      let dragSrc = null;
      timeline.addEventListener('dragstart', function(e) {
        const li = e.target.closest('.timeline-item');
        if (!li) return;
        dragSrc = li;
        e.dataTransfer.effectAllowed = 'move';
        li.style.opacity = '0.4';
      });
      timeline.addEventListener('dragend', function(e) { if (dragSrc) dragSrc.style.opacity = '1'; dragSrc = null; });
      timeline.addEventListener('dragover', function(e) { e.preventDefault(); });
      timeline.addEventListener('drop', function(e) {
        e.preventDefault();
        const target = e.target.closest('.timeline-item');
        if (!target || !dragSrc || target === dragSrc) return;
        // determine position
        const rect = target.getBoundingClientRect();
        const mid = rect.left + rect.width/2;
        if (e.clientX < mid) timeline.insertBefore(dragSrc, target);
        else timeline.insertBefore(dragSrc, target.nextSibling);
        // rerender widths
        renderTimeline(timeline);
      });

      // click to edit delay seconds
      timeline.addEventListener('dblclick', function(e) {
        const block = e.target.closest('.timeline-item');
        if (!block || block.dataset.type !== 'delay') return;
        const current = parseFloat(block.dataset.seconds || 0);
        const val = prompt('Set delay seconds:', current);
        if (val === null) return;
        const secs = parseFloat(val) || 0;
        block.dataset.seconds = secs;
        block.dataset.duration = secs;
        block.querySelector('.meta').textContent = secs.toFixed(1) + 's';
        renderTimeline(timeline);
      });

      // remove handler (right-click)
      timeline.addEventListener('contextmenu', function(e) {
        const block = e.target.closest('.timeline-item');
        if (!block) return;
        e.preventDefault();
        if (confirm('Remove this item?')) { block.remove(); renderTimeline(timeline); }
      });

      // Add delay button
      form.querySelector('.add-delay-btn').addEventListener('click', function() {
        const block = createBlock({ type: 'delay', seconds: 1.0 });
        timeline.appendChild(block);
        renderTimeline(timeline);
      });

      // File upload handler (AJAX)
      const fileInput = form.querySelector('.upload-input');
      fileInput.addEventListener('change', function() {
        if (!this.files.length) return;
        const file = this.files[0];
        const formData = new FormData();
        formData.append('guest_id', guestId);
        formData.append('file', file);
        fetch('/upload', { method: 'POST', body: formData })
        .then(resp => resp.json())
        .then(data => {
          if (data && data.status === 'ok') {
            const duration = data.duration || 0;
            timeline.appendChild(createBlock({ type: 'track', track: data.filename, duration: duration }));
            timeline.appendChild(createBlock({ type: 'delay', seconds: 1.0, duration: 1.0 }));
            renderTimeline(timeline);
          } else {
            alert('Upload failed');
          }
        })
        .catch(() => alert('Network error'));
        this.value = '';
      });

      // Submit handler - converts timeline to playlist array
      form.addEventListener('submit', function(e) {
        e.preventDefault();
        const items = [];
        timeline.querySelectorAll('.timeline-item').forEach(function(div) {
          if (div.dataset.type === 'track') {
            items.push({ type: 'track', track: div.dataset.track, duration: parseFloat(div.dataset.duration || 0) });
          } else if (div.dataset.type === 'delay') {
            const secs = parseFloat(div.dataset.seconds || 0);
            items.push({ type: 'delay', seconds: secs });
          }
        });
        fetch('/playlist', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ guest_id: guestId, playlist: items })
        }).then(resp => resp.ok ? alert('Playlist saved') : alert('Error saving playlist')).catch(() => alert('Network error'));
      });

      // initial render
      renderTimeline(timeline);
      // adjust on resize
      window.addEventListener('resize', function() { renderTimeline(timeline); });
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
        if request.headers.get('Accept', '').find('application/json') >= 0 or 'fetch' in request.headers.get('Sec-Fetch-Mode', ''):
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
        # Validate and clean playlist items
        entries = []
        for item in playlist:
            if item.get('type') == 'track' and item.get('track'):
                entries.append({'type': 'track', 'track': item['track']})
            elif item.get('type') == 'delay':
                entries.append({'type': 'delay', 'seconds': float(item.get('seconds', 0))})
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
                    entries.append({'type': 'track', 'track': item['track']})
                elif item.get('type') == 'delay':
                    entries.append({'type': 'delay', 'seconds': float(item.get('seconds', 0))})
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
