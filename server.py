import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import get_all_vessels, get_vessel_detail, get_vessel_track, get_avg_speed, get_stats
from currents import get_all_currents
from sfbofs import get_current_field
from wind import get_wind_field
from tides import get_all_tide_heights

logger = logging.getLogger(__name__)

app = FastAPI(title="AIS Tracker")
app.add_middleware(GZipMiddleware, minimum_size=1000)

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


@app.get("/sw.js")
async def service_worker():
    """Serve SW from root so it can control the whole site."""
    return FileResponse(static_dir / "sw.js", media_type="application/javascript")


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


def _parse_target_time(minutes_offset: int | None) -> datetime | None:
    """Convert minutes-from-now offset to UTC datetime. None/0 = real-time."""
    if not minutes_offset:
        return None
    return datetime.now(timezone.utc) + timedelta(minutes=minutes_offset)


def _compute_forecast_hour(minutes_offset: int | None) -> int:
    """Convert minutes offset to whole forecast hours (capped at 48 for model data)."""
    if not minutes_offset:
        return 0
    return min(max(0, minutes_offset // 60), 48)


@app.get("/api/currents")
async def api_currents(time: int | None = None):
    target = _parse_target_time(time)
    return await get_all_currents(target_time=target)


@app.get("/api/current-field")
async def api_current_field(time: int | None = None):
    fh = _compute_forecast_hour(time)
    field = await get_current_field(forecast_hour=fh)
    if field:
        return field
    return {"error": "SFBOFS data not available"}


@app.get("/api/wind-field")
async def api_wind_field(time: int | None = None):
    fh = _compute_forecast_hour(time)
    field = await get_wind_field(forecast_hour=fh)
    if field:
        return field
    return {"error": "Wind data not available"}


@app.get("/api/tide-height")
async def api_tide_height(time: int | None = None):
    target = _parse_target_time(time)
    return await get_all_tide_heights(target_time=target)


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
    for ws in list(ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.add(ws)
    for ws in disconnected:
        ws_clients.discard(ws)
