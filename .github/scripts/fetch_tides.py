#!/usr/bin/env python3
"""
Fetch NOAA tide height predictions for 14 SF Bay stations.

Fetches 3 days of predictions (today + 2 days) so the 48h forecast
timeline always has data. Runs 2x/day.
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATIONS = {
    "9414290": {"name": "San Francisco (Golden Gate)", "lat": 37.8063, "lon": -122.4659},
    "9414750": {"name": "Alameda", "lat": 37.7720, "lon": -122.3003},
    "9414764": {"name": "Oakland Inner Harbor", "lat": 37.7950, "lon": -122.2820},
    "9414816": {"name": "Berkeley", "lat": 37.8650, "lon": -122.3070},
    "9414874": {"name": "Corte Madera Creek", "lat": 37.9433, "lon": -122.5130},
    "9414688": {"name": "San Leandro Marina", "lat": 37.6950, "lon": -122.1920},
    "9414523": {"name": "Redwood City", "lat": 37.5068, "lon": -122.2119},
    "9414458": {"name": "San Mateo Bridge (West)", "lat": 37.5800, "lon": -122.2530},
    "9414509": {"name": "Dumbarton Bridge", "lat": 37.5067, "lon": -122.1150},
    "9414131": {"name": "Half Moon Bay", "lat": 37.5025, "lon": -122.4822},
    "9414863": {"name": "Richmond (Chevron Pier)", "lat": 37.9283, "lon": -122.4000},
    "9415056": {"name": "Pinole Point", "lat": 38.0150, "lon": -122.3630},
    "9415102": {"name": "Martinez", "lat": 38.0346, "lon": -122.1252},
    "9415144": {"name": "Port Chicago", "lat": 38.0560, "lon": -122.0395},
}

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "tides"


def _fetch_url(url: str) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def _build_url(station_id: str, begin_date: str, end_date: str) -> str:
    return (
        f"{NOAA_BASE}?begin_date={begin_date}&end_date={end_date}"
        f"&station={station_id}&product=predictions&datum=MLLW"
        f"&units=english&time_zone=gmt&format=json&interval=6"
    )


def main():
    now = datetime.now(timezone.utc)
    begin = now.strftime("%Y%m%d")
    end = (now + timedelta(days=3)).strftime("%Y%m%d")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0

    for station_id, info in STATIONS.items():
        url = _build_url(station_id, begin, end)
        text = _fetch_url(url)
        if not text:
            logger.warning(f"Failed to fetch tides for {station_id}")
            continue

        try:
            parsed = json.loads(text)
            predictions = parsed.get("predictions")
            if not predictions:
                logger.warning(f"No predictions in response for {station_id}")
                continue

            # Save with station metadata embedded
            output = {
                "station_id": station_id,
                "name": info["name"],
                "lat": info["lat"],
                "lon": info["lon"],
                "predictions": predictions,
            }

            out_path = OUTPUT_DIR / f"{station_id}.json"
            out_path.write_text(json.dumps(output))
            saved += 1
            logger.info(f"Saved {station_id} ({info['name']}): {len(predictions)} predictions")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Parse error for {station_id}: {e}")

    logger.info(f"Tides complete: {saved}/{len(STATIONS)} stations")

    # Update meta
    meta_path = OUTPUT_DIR.parent / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["tides_updated"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))

    if saved == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
