import asyncio
import logging
import sys

import uvicorn

from config import AIS_HOST, AIS_PORT, SERVER_HOST, SERVER_PORT
from database import init_db, upsert_vessel, insert_position
from ais_listener import ais_listener
from ais_decoder import ais_decoder
from server import app, set_db, broadcast, increment_message_count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ais-tracker")


# --- Demo mode: generate fake AIS data for testing without a receiver ---

DEMO_VESSELS = [
    {"mmsi": 338361814, "shipname": "OWN VESSEL", "ship_type": 36, "ship_category": "Sailing/Pleasure",
     "lat": 37.8085, "lon": -122.4095, "sog": 5.2, "cog": 245.0, "heading": 243},
    {"mmsi": 367596000, "shipname": "GOLDEN GATE FERRY", "ship_type": 60, "ship_category": "Passenger",
     "lat": 37.8120, "lon": -122.4200, "sog": 12.5, "cog": 180.0, "heading": 178},
    {"mmsi": 366814480, "shipname": "PACIFIC TRADER", "ship_type": 70, "ship_category": "Cargo",
     "lat": 37.8200, "lon": -122.3900, "sog": 8.3, "cog": 310.0, "heading": 308},
    {"mmsi": 211234567, "shipname": "WANDERER III", "ship_type": 36, "ship_category": "Sailing/Pleasure",
     "lat": 37.8050, "lon": -122.4150, "sog": 3.1, "cog": 120.0, "heading": 118},
    {"mmsi": 441234000, "shipname": "OCEAN SPIRIT", "ship_type": 80, "ship_category": "Tanker",
     "lat": 37.8300, "lon": -122.3800, "sog": 10.7, "cog": 275.0, "heading": 273},
    {"mmsi": 563456789, "shipname": "BAY EXPLORER", "ship_type": 36, "ship_category": "Sailing/Pleasure",
     "lat": 37.7980, "lon": -122.4250, "sog": 4.5, "cog": 45.0, "heading": 42},
]

import math
import random


async def demo_listener(decoded_queue: asyncio.Queue):
    """Generate simulated vessel movements for demo/testing."""
    logger.info("Demo mode: generating simulated AIS data")
    vessels = [dict(v) for v in DEMO_VESSELS]
    from datetime import datetime, timezone

    while True:
        for v in vessels:
            # Simulate movement
            speed_nm_per_sec = v["sog"] / 3600.0
            cog_rad = math.radians(v["cog"])
            v["lat"] += speed_nm_per_sec / 60.0 * math.cos(cog_rad) + random.gauss(0, 0.00001)
            v["lon"] += speed_nm_per_sec / 60.0 * math.sin(cog_rad) / math.cos(math.radians(v["lat"])) + random.gauss(0, 0.00001)
            v["sog"] = max(0, v["sog"] + random.gauss(0, 0.2))
            v["cog"] = (v["cog"] + random.gauss(0, 1.0)) % 360
            v["heading"] = int(v["cog"] + random.gauss(0, 2)) % 360
            v["timestamp"] = datetime.now(timezone.utc).isoformat()
            v["is_own_vessel"] = v["mmsi"] == 338361814

            await decoded_queue.put(dict(v))

        await asyncio.sleep(2)


async def process_decoded(db, decoded_queue: asyncio.Queue):
    """Process decoded AIS messages: store in DB and broadcast via WebSocket."""
    while True:
        data = await decoded_queue.get()
        increment_message_count()

        try:
            await upsert_vessel(db, data)
            await insert_position(db, data)
            from database import get_avg_speed
            data["avg_speed"] = await get_avg_speed(db, data["mmsi"])
            name = data.get("shipname") or data.get("name") or f"MMSI {data['mmsi']}"
            lat = data.get("lat", "?")
            lon = data.get("lon", "?")
            sog = data.get("sog", "?")
            logger.info(f"VESSEL: {name} | {lat},{lon} | {sog} kn | msg_type={data.get('msg_type', '?')}")
            await broadcast(data)
        except Exception as e:
            logger.error(f"Error processing message: {e}")


async def main():
    demo_mode = "--demo" in sys.argv

    db = await init_db()
    set_db(db)

    raw_queue = asyncio.Queue()
    decoded_queue = asyncio.Queue()

    if demo_mode:
        logger.info("Starting in DEMO mode (simulated AIS data)")
        asyncio.create_task(demo_listener(decoded_queue))
    else:
        from config import AIS_PROTOCOL
        logger.info(f"Starting AIS listener → {AIS_HOST}:{AIS_PORT} (protocol={AIS_PROTOCOL})")
        asyncio.create_task(ais_listener(raw_queue, protocol=AIS_PROTOCOL))
        asyncio.create_task(ais_decoder(raw_queue, decoded_queue))

    asyncio.create_task(process_decoded(db, decoded_queue))

    logger.info(f"Starting web server on http://{SERVER_HOST}:{SERVER_PORT}")
    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
