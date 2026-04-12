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

# Verbose vessel logging (use --verbose to enable)
VERBOSE_VESSELS = False


# --- Demo mode: generate fake AIS data for testing without a receiver ---

DEMO_VESSELS = [
    {"mmsi": 338361814, "shipname": "TYPON", "ship_type": 36, "ship_category": "Sailing/Pleasure",
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
            if VERBOSE_VESSELS:
                lat = data.get("lat", "?")
                lon = data.get("lon", "?")
                sog = data.get("sog", "?")
                logger.info(f"VESSEL: {name} | {lat},{lon} | {sog} kn | msg_type={data.get('msg_type', '?')}")
            await broadcast(data)
        except Exception as e:
            logger.error(f"Error processing message: {e}")


async def try_local_ais(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if a local AIS receiver is reachable."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (ConnectionRefusedError, ConnectionResetError, OSError, asyncio.TimeoutError):
        return False


async def refresh_environmental_data():
    """Pre-fetch wind, current field, tidal currents, and tide data on startup and periodically.

    Runs immediately on startup to replace stale offline caches,
    then repeats every 30 minutes to keep forecast data fresh.
    """
    from wind import get_wind_field, save_forecast_cache as save_wind_cache, load_forecast_cache as load_wind_cache, GRID_CACHE_TTL
    from tides import get_all_tide_heights, save_offline_cache as save_tide_cache
    from sfbofs import get_current_field, save_forecast_cache as save_sfbofs_cache, load_forecast_cache as load_sfbofs_cache
    from currents import get_all_currents, save_offline_cache as save_currents_cache
    from datetime import datetime, timezone, timedelta

    # Load cached forecast data from disk immediately (serves stale data while refreshing)
    load_wind_cache()
    load_sfbofs_cache()
    logger.info("Loaded forecast caches from disk (if available)")

    while True:
        logger.info("Refreshing environmental data (wind + currents + tides)...")
        try:
            # Wind: current + next 48 forecast hours
            wind_ok = 0
            for hour in range(0, 49):
                try:
                    result = await get_wind_field(forecast_hour=hour)
                    if result:
                        wind_ok += 1
                except Exception as e:
                    logger.debug(f"Wind forecast hour {hour} failed: {e}")
            logger.info(f"Wind data refreshed: {wind_ok}/49 hours OK")
            save_wind_cache()

            # Tide heights: fetch today + tomorrow for all 14 stations
            try:
                tides = await get_all_tide_heights()
                logger.info(f"Tide data refreshed: {len(tides)} stations")
                saved = await save_tide_cache()
                logger.info(f"Tide offline cache saved: {saved} stations")
            except Exception as e:
                logger.warning(f"Tide refresh failed: {e}")

            # Tidal currents: fetch today + tomorrow for all 6 stations
            try:
                now = datetime.now(timezone.utc)
                tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
                currents = await get_all_currents()
                # Also pre-fetch tomorrow's predictions
                currents_tomorrow = await get_all_currents(target_time=now + timedelta(days=1))
                logger.info(f"Tidal currents refreshed: {len(currents)} stations today, {len(currents_tomorrow)} tomorrow")
                saved = await save_currents_cache()
                logger.info(f"Currents offline cache saved: {saved} stations")
            except Exception as e:
                logger.warning(f"Tidal currents refresh failed: {e}")

            # Current field: current + next 48 forecast hours
            cf_ok = 0
            for hour in range(0, 49):
                try:
                    result = await get_current_field(forecast_hour=hour)
                    if result:
                        cf_ok += 1
                except Exception as e:
                    logger.debug(f"Current field forecast hour {hour} failed: {e}")
            logger.info(f"Current field refreshed: {cf_ok}/49 hours OK")
            save_sfbofs_cache()

        except Exception as e:
            logger.warning(f"Environmental data refresh error: {e}")

        # Sleep for the wind cache TTL (30 min) before next refresh
        await asyncio.sleep(GRID_CACHE_TTL)


async def main():
    demo_mode = "--demo" in sys.argv
    aisstream_mode = "--aisstream" in sys.argv
    local_mode = "--local" in sys.argv
    global VERBOSE_VESSELS
    VERBOSE_VESSELS = "--verbose" in sys.argv

    db = await init_db()
    set_db(db)

    raw_queue = asyncio.Queue()
    decoded_queue = asyncio.Queue()

    if demo_mode:
        logger.info("Starting in DEMO mode (simulated AIS data)")
        asyncio.create_task(demo_listener(decoded_queue))
    elif aisstream_mode:
        from config import AISSTREAM_API_KEY
        from aisstream_listener import aisstream_listener
        if not AISSTREAM_API_KEY:
            logger.error("AISSTREAM_API_KEY not set. Set it in config.py or env var.")
            sys.exit(1)
        logger.info("Starting in AISSTREAM mode (cloud AIS data from aisstream.io)")
        asyncio.create_task(aisstream_listener(decoded_queue, AISSTREAM_API_KEY))
    elif local_mode:
        from config import AIS_PROTOCOL
        logger.info(f"Starting AIS listener → {AIS_HOST}:{AIS_PORT} (protocol={AIS_PROTOCOL})")
        asyncio.create_task(ais_listener(raw_queue, protocol=AIS_PROTOCOL))
        asyncio.create_task(ais_decoder(raw_queue, decoded_queue))
    else:
        # Auto mode: try local AIS receiver first, fall back to AISstream
        logger.info(f"Auto-detecting AIS source: trying local receiver at {AIS_HOST}:{AIS_PORT}...")
        if await try_local_ais(AIS_HOST, AIS_PORT):
            from config import AIS_PROTOCOL
            logger.info("Local AIS receiver found — using local mode")
            asyncio.create_task(ais_listener(raw_queue, protocol=AIS_PROTOCOL))
            asyncio.create_task(ais_decoder(raw_queue, decoded_queue))
        else:
            from config import AISSTREAM_API_KEY
            if AISSTREAM_API_KEY:
                from aisstream_listener import aisstream_listener
                logger.info("No local AIS receiver — falling back to AISstream.io")
                asyncio.create_task(aisstream_listener(decoded_queue, AISSTREAM_API_KEY))
            else:
                logger.warning("No local AIS receiver and no AISSTREAM_API_KEY set — no AIS data source available")
                logger.info("Use --demo for simulated data, or set AISSTREAM_API_KEY in .env")

    asyncio.create_task(process_decoded(db, decoded_queue))
    asyncio.create_task(refresh_environmental_data())

    logger.info(f"Starting web server on http://{SERVER_HOST}:{SERVER_PORT}")
    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
