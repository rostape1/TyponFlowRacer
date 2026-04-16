#!/usr/bin/env python3
"""
Fetch NOAA tidal current predictions for 6 SF Bay stations.

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
    "SFB1201": {"name": "Golden Gate Bridge", "lat": 37.8117, "lon": -122.4717},
    "SFB1203": {"name": "Alcatraz (North)", "lat": 37.8317, "lon": -122.4217},
    "SFB1204": {"name": "Alcatraz (South)", "lat": 37.8183, "lon": -122.4200},
    "SFB1205": {"name": "Angel Island (East)", "lat": 37.8633, "lon": -122.4217},
    "SFB1206": {"name": "Raccoon Strait", "lat": 37.8567, "lon": -122.4467},
    "SFB1211": {"name": "Bay Bridge", "lat": 37.8033, "lon": -122.3633},
}

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "currents"


def _fetch_url(url: str) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def _build_url(station_id: str, date: str) -> str:
    return (
        f"{NOAA_BASE}?date={date}&station={station_id}"
        f"&product=currents_predictions&units=english"
        f"&time_zone=gmt&format=json&interval=6"
    )


def main():
    now = datetime.now(timezone.utc)
    dates = [
        now.strftime("%Y%m%d"),
        (now + timedelta(days=1)).strftime("%Y%m%d"),
        (now + timedelta(days=2)).strftime("%Y%m%d"),
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0

    for station_id, info in STATIONS.items():
        all_predictions = []
        for date_str in dates:
            url = _build_url(station_id, date_str)
            text = _fetch_url(url)
            if not text:
                continue
            try:
                parsed = json.loads(text)
                predictions = None
                if "current_predictions" in parsed:
                    predictions = parsed["current_predictions"].get("cp", [])
                elif "predictions" in parsed:
                    predictions = parsed["predictions"]
                if predictions:
                    all_predictions.extend(predictions)
            except (json.JSONDecodeError, KeyError) as e:
                logger.debug(f"Parse error for {station_id} on {date_str}: {e}")

        if not all_predictions:
            logger.warning(f"No predictions for {station_id}")
            continue

        # Deduplicate by Time field
        seen = set()
        unique = []
        for p in all_predictions:
            t = p.get("Time", "")
            if t not in seen:
                seen.add(t)
                unique.append(p)

        # Save with station metadata embedded
        output = {
            "station_id": station_id,
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "predictions": unique,
        }

        out_path = OUTPUT_DIR / f"{station_id}.json"
        out_path.write_text(json.dumps(output))
        saved += 1
        logger.info(f"Saved {station_id} ({info['name']}): {len(unique)} predictions")

    logger.info(f"Currents complete: {saved}/{len(STATIONS)} stations")

    # Update meta
    meta_path = OUTPUT_DIR.parent / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["currents_updated"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))

    if saved == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
