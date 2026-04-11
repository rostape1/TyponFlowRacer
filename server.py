import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import get_all_vessels, get_vessel_detail, get_vessel_track, get_avg_speed, get_stats
from currents import get_all_currents

logger = logging.getLogger(__name__)

app = FastAPI(title="AIS Tracker")

# WebSocket clients
ws_clients: set[WebSocket] = set()

# DB reference — set by main.py before startup
db = None

# Message counter
message_count = 0


def set_db(database):
    global db
    db = database


def increment_message_count():
    global message_count
    message_count += 1


# Serve static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/vessels")
async def api_vessels():
    vessels = await get_all_vessels(db)
    # Add avg speed for each vessel
    for v in vessels:
        v["avg_speed"] = await get_avg_speed(db, v["mmsi"])
    return vessels


@app.get("/api/vessels/{mmsi}")
async def api_vessel(mmsi: int):
    vessel = await get_vessel_detail(db, mmsi)
    if vessel:
        vessel["avg_speed"] = await get_avg_speed(db, mmsi)
    return vessel or {"error": "not found"}


@app.get("/api/vessels/{mmsi}/track")
async def api_vessel_track(mmsi: int, hours: float | None = 2.0):
    track = await get_vessel_track(db, mmsi, hours)
    return track


@app.get("/api/stats")
async def api_stats():
    stats = await get_stats(db)
    stats["message_count"] = message_count
    return stats


@app.get("/api/currents")
async def api_currents():
    return await get_all_currents()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    logger.info(f"WebSocket client connected ({len(ws_clients)} total)")
    try:
        while True:
            # Keep connection alive; ignore client messages
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
        logger.info(f"WebSocket client disconnected ({len(ws_clients)} total)")


async def broadcast(data: dict):
    """Push a vessel update to all connected WebSocket clients."""
    if not ws_clients:
        return
    payload = json.dumps(data)
    disconnected = set()
    for ws in ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.add(ws)
    for ws in disconnected:
        ws_clients.discard(ws)
