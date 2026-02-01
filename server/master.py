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
import websockets
from aiohttp import web
import aiohttp
import socket

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

HOST = _get_local_ip()
PORT = 8765
LEAD_TIME = 5.0        # seconds before playback
PLAYLIST = {          # guest_id: track_filename
    '1': 'track1.mp3',
    '2': 'track2.mp3',
    # ... up to '16'
}

clients = {}  # guest_id -> websocket connection

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
    t0 = time.time() + LEAD_TIME
    print(f"Scheduling PLAY at {t0} (+{LEAD_TIME}s)")
    # send play commands
    for guest_id, track in PLAYLIST.items():
        ws = clients.get(guest_id)
        if not ws:
            print(f"Warning: guest {guest_id} not connected")
            continue
        cmd = {
            'type': 'PLAY',
            'track': track,
            'timestamp': t0,
            'offset': 0.0
        }
        await ws.send(json.dumps(cmd))
    print("PLAY commands dispatched.")

def signal_handler(sig, frame):
    print("Shutting down master...")
    sys.exit(0)

# HTTP and WS server on same port
HTTP_PORT = 8080
ALIAS = 'willowcrestmanor.local'

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
            'body { font-family: sans-serif; background: #f5f5f5; margin:0; padding:0; }'
            '.container { max-width:800px; margin:40px auto; background:#fff; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.1); }'
            'h1 { text-align:center; color:#333; }'
            'h2 { color:#444; margin-top:30px; }'
            'ul { list-style:none; padding:0; }'
            'li { padding:8px 0; border-bottom:1px solid #eee; }'
            'form { margin-top:20px; }'
            'input[type=text], input[type=file] { width:100%; padding:8px; margin:6px 0; box-sizing:border-box; }'
            'button { background:#0066cc; color:#fff; border:none; padding:10px 20px; border-radius:4px; cursor:pointer; }'
            'button:hover { background:#005bb5; }'
            '</style>'
            '</head>'
            '<body><div class="container">')
    html += '<h1>Willowcrest Manor Audio Master Control</h1>'
    html += '<h2>Connected Guests</h2><ul>'
    for g in guests:
        html += f'<li>Guest {g} - Assigned: {PLAYLIST.get(g, "None")}</li>'
    html += '</ul>'

    # upload form
    html += '<h2>Upload & Assign Track</h2>'
    html += '<form method="POST" action="/upload" enctype="multipart/form-data">'
    html += 'Guest ID: <input name="guest_id" /> <br/>'
    html += 'File: <input type="file" name="file" accept=".mp3" /> <br/>'
    html += '<button type="submit">Upload</button>'
    html += '</form>'

    # start show button
    html += '<h2>Start Show</h2>'
    html += '<form method="POST" action="/start">'
    html += '<button type="submit">GO</button>'
    html += '</form>'

    html += '</div></body></html>'
    return web.Response(text=html, content_type='text/html')

async def upload_handler(request):
    reader = await request.multipart()
    field = await reader.next()
    guest_id = (await field.text()).strip()
    field = await reader.next()
    if field.name == 'file':
        fname = f'{guest_id}.mp3'
        path = AUDIO_DIR / fname
        with open(path, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        PLAYLIST[guest_id] = fname
    return web.HTTPFound('/')

async def start_handler(request):
    await start_show()
    return web.HTTPFound('/')

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    # register
    msg = await ws.receive_str()
    data = json.loads(msg)
    gid = data.get('guest_id')
    if gid:
        clients[gid] = ws
    try:
        async for msg in ws:
            pass
    finally:
        clients.pop(gid, None)
    return ws

# setup aiohttp app
app = web.Application()
app.router.add_get('/', index)
app.router.add_post('/upload', upload_handler)
app.router.add_post('/start', start_handler)
app.router.add_get('/ws', ws_handler)

if __name__ == '__main__':
    print(f'Starting HTTP+WS server on port {HTTP_PORT}')
    print(f'Access web UI at: http://{ALIAS}:{HTTP_PORT}')
    web.run_app(app, host=HOST, port=HTTP_PORT)

# disable old console main
if False:
    asyncio.run(main())
