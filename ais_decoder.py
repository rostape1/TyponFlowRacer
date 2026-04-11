import asyncio
import logging
from datetime import datetime, timezone

from pyais import decode

from config import OWN_MMSI

logger = logging.getLogger(__name__)

# AIS ship type ranges → human-readable categories
SHIP_TYPE_MAP = {
    range(20, 30): "Wing in Ground",
    range(30, 36): "Fishing/Towing/Dredging",
    range(36, 40): "Sailing/Pleasure",
    range(40, 50): "High Speed Craft",
    range(50, 60): "Special Craft",
    range(60, 70): "Passenger",
    range(70, 80): "Cargo",
    range(80, 90): "Tanker",
    range(90, 100): "Other",
}


def get_ship_category(ship_type: int | None) -> str:
    if ship_type is None:
        return "Unknown"
    for type_range, category in SHIP_TYPE_MAP.items():
        if ship_type in type_range:
            return category
    return "Other"


def decode_ais_message(sentence: str) -> dict | None:
    """Decode a single NMEA sentence. Returns a dict or None if not decodable."""
    try:
        decoded = decode(sentence)
        msg = decoded.asdict()

        result = {
            "mmsi": msg.get("mmsi"),
            "msg_type": msg.get("msg_type"),
            "is_own_vessel": msg.get("mmsi") == OWN_MMSI,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Position reports (msg types 1, 2, 3, 18, 19)
        lat = msg.get("lat")
        lon = msg.get("lon")
        if lat is not None and lon is not None:
            # AIS uses 91.0 / 181.0 as "not available"
            if abs(lat) <= 90.0 and abs(lon) <= 180.0:
                result["lat"] = round(lat, 6)
                result["lon"] = round(lon, 6)

        sog = msg.get("speed")
        if sog is not None and sog < 102.3:  # 102.3 = not available
            result["sog"] = round(sog, 1)

        cog = msg.get("course")
        if cog is not None and cog < 360.0:
            result["cog"] = round(cog, 1)

        heading = msg.get("heading")
        if heading is not None and heading < 360:
            result["heading"] = heading

        # Static data (msg types 5, 24)
        for field in ("shipname", "destination", "ship_type", "to_bow", "to_stern", "to_port", "to_starboard"):
            val = msg.get(field)
            if val is not None:
                result[field] = val

        # Compute length/beam from dimension fields
        if "to_bow" in result and "to_stern" in result:
            result["length"] = result["to_bow"] + result["to_stern"]
        if "to_port" in result and "to_starboard" in result:
            result["beam"] = result["to_port"] + result["to_starboard"]

        if result.get("ship_type") is not None:
            result["ship_category"] = get_ship_category(result["ship_type"])

        if result["mmsi"] is None:
            return None

        return result

    except Exception as e:
        logger.debug(f"Failed to decode: {sentence!r} — {e}")
        return None


async def ais_decoder(raw_queue: asyncio.Queue, decoded_queue: asyncio.Queue):
    """Read raw NMEA sentences, decode them, and push structured data to decoded_queue."""
    # Buffer for multi-part messages
    fragment_buffer: dict[tuple, list[str]] = {}

    while True:
        sentence = await raw_queue.get()

        try:
            # Check if multi-part message
            parts = sentence.split(",")
            if len(parts) >= 3 and parts[0] in ("!AIVDM", "!AIVDO"):
                total_frags = int(parts[1])
                frag_num = int(parts[2])

                if total_frags == 1:
                    # Single-part message — decode directly
                    result = decode_ais_message(sentence)
                    if result:
                        await decoded_queue.put(result)
                else:
                    # Multi-part: buffer fragments
                    seq_id = parts[3] if parts[3] else "0"
                    key = (parts[0], seq_id)

                    if frag_num == 1:
                        fragment_buffer[key] = [sentence]
                    elif key in fragment_buffer:
                        fragment_buffer[key].append(sentence)

                        if frag_num == total_frags:
                            # All fragments received — decode combined
                            combined = "\n".join(fragment_buffer.pop(key))
                            result = decode_ais_message(combined)
                            if result:
                                await decoded_queue.put(result)

        except Exception as e:
            logger.debug(f"Decoder error for {sentence!r}: {e}")
