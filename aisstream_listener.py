import asyncio
import json
import logging
from datetime import datetime, timezone

from ais_decoder import get_ship_category
from config import OWN_MMSI

logger = logging.getLogger(__name__)

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

# AISstream message type mapping to AIS msg_type numbers
MSG_TYPE_MAP = {
    "PositionReport": 1,
    "StandardClassBPositionReport": 18,
    "ExtendedClassBPositionReport": 19,
    "StaticDataReport": 5,
    "ShipStaticData": 5,
    "StandardSearchAndRescueAircraftReport": 9,
    "AidsToNavigationReport": 21,
}


def parse_aisstream_message(raw: dict) -> dict | None:
    """Convert an AISstream.io JSON message to our internal format."""
    try:
        meta = raw.get("MetaData", {})
        msg_type_name = raw.get("MessageType", "")
        message = raw.get("Message", {})

        mmsi = meta.get("MMSI")
        if not mmsi:
            return None

        result = {
            "mmsi": mmsi,
            "msg_type": MSG_TYPE_MAP.get(msg_type_name, 0),
            "is_own_vessel": mmsi == OWN_MMSI,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "name": (meta.get("ShipName") or "").strip() or None,
        }

        # Get the actual message payload (nested under the message type key)
        payload = message.get(msg_type_name, {})

        # Position data
        lat = payload.get("Latitude")
        lon = payload.get("Longitude")
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            result["lat"] = round(lat, 6)
            result["lon"] = round(lon, 6)

        sog = payload.get("Sog")
        if sog is not None and sog < 102.3:
            result["sog"] = round(sog, 1)

        cog = payload.get("Cog")
        if cog is not None and cog < 360.0:
            result["cog"] = round(cog, 1)

        heading = payload.get("TrueHeading")
        if heading is not None and heading < 360:
            result["heading"] = heading

        # Static data
        ship_type = payload.get("Type")
        if ship_type is not None:
            result["ship_type"] = ship_type
            result["ship_category"] = get_ship_category(ship_type)

        dest = payload.get("Destination")
        if dest and dest.strip():
            result["destination"] = dest.strip()

        shipname = payload.get("ShipName")
        if shipname and shipname.strip():
            result["shipname"] = shipname.strip()

        # Dimensions
        dim = payload.get("Dimension", {})
        if dim:
            a = dim.get("A", 0)
            b = dim.get("B", 0)
            c = dim.get("C", 0)
            d = dim.get("D", 0)
            if a + b > 0:
                result["length"] = a + b
                result["to_bow"] = a
                result["to_stern"] = b
            if c + d > 0:
                result["beam"] = c + d
                result["to_port"] = c
                result["to_starboard"] = d

        return result

    except Exception as e:
        logger.debug(f"Failed to parse AISstream message: {e}")
        return None


async def aisstream_listener(decoded_queue: asyncio.Queue, api_key: str, bbox: list[list[float]] = None):
    """
    Connect to AISstream.io WebSocket and push decoded vessel data.

    bbox: [[lat_min, lon_min], [lat_max, lon_max]] — area to subscribe to.
          Defaults to SF Bay area.
    """
    if bbox is None:
        # Default: SF Bay and surrounding waters
        bbox = [[37.4, -122.8], [38.2, -122.0]]

    try:
        import websockets
    except ImportError:
        logger.error("websockets package required for AISstream mode: pip install websockets")
        return

    subscribe_msg = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [bbox],
    })

    while True:
        try:
            logger.info(f"Connecting to AISstream.io...")
            async with websockets.connect(AISSTREAM_URL) as ws:
                await ws.send(subscribe_msg)
                logger.info("Connected to AISstream.io — receiving vessel data")

                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                        result = parse_aisstream_message(data)
                        if result:
                            await decoded_queue.put(result)
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            logger.warning(f"AISstream connection error: {e}")

        logger.info("Reconnecting to AISstream.io in 5 seconds...")
        await asyncio.sleep(5)
