#!/usr/bin/env python3
"""
Fetch real-time NDBC buoy observations and output stations.json.

Lightweight job — just 9 small text file downloads. Runs every 10 minutes.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MS_TO_KN = 1.94384

NDBC_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"

NDBC_STATIONS = {
    "FTPC1": {"name": "Fort Point", "lat": 37.8060, "lon": -122.4659},
    "SFXC1": {"name": "SF Bar Pilots", "lat": 37.7600, "lon": -122.6900},
    "RCMC1": {"name": "Richmond", "lat": 37.9228, "lon": -122.4098},
    "AAMC1": {"name": "Alameda", "lat": 37.7717, "lon": -122.2992},
    "OKXC1": {"name": "Oakland", "lat": 37.8067, "lon": -122.3340},
    "PPXC1": {"name": "Point Potrero", "lat": 37.9078, "lon": -122.3728},
    "46026": {"name": "SF Buoy (offshore)", "lat": 37.7590, "lon": -122.8330},
    "TIBC1": {"name": "Tiburon", "lat": 37.8911, "lon": -122.4478},
    "46012": {"name": "Half Moon Bay", "lat": 37.3630, "lon": -122.8810},
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "wind"


def _fetch_url(url: str, timeout: int = 10) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def main():
    stations = []

    for station_id, info in NDBC_STATIONS.items():
        url = NDBC_URL.format(station=station_id)
        text = _fetch_url(url)
        if not text:
            continue

        try:
            lines = text.strip().split("\n")
            if len(lines) < 3:
                continue

            header = lines[0].replace("#", "").split()
            data_line = lines[2].split()

            col = {h: i for i, h in enumerate(header)}

            wdir_idx = col.get("WDIR")
            wspd_idx = col.get("WSPD")
            gst_idx = col.get("GST")

            if wdir_idx is None or wspd_idx is None:
                continue

            wdir_str = data_line[wdir_idx]
            wspd_str = data_line[wspd_idx]
            gst_str = data_line[gst_idx] if gst_idx is not None else "MM"

            if wdir_str == "MM" or wspd_str == "MM":
                continue

            speed_kn = float(wspd_str) * MS_TO_KN
            gust_kn = float(gst_str) * MS_TO_KN if gst_str != "MM" else None
            direction = float(wdir_str)

            yr = data_line[col["YY"]]
            mo = data_line[col["MM"]]
            dy = data_line[col["DD"]]
            hh = data_line[col["hh"]]
            mm = data_line[col["mm"]]
            timestamp = f"{yr}-{mo}-{dy}T{hh}:{mm}Z"

            stations.append({
                "id": station_id,
                "name": info["name"],
                "lat": info["lat"],
                "lon": info["lon"],
                "speed_kn": round(speed_kn, 1),
                "gust_kn": round(gust_kn, 1) if gust_kn is not None else None,
                "direction": direction,
                "timestamp": timestamp,
            })
        except (ValueError, IndexError) as e:
            logger.debug(f"Parse error for {station_id}: {e}")

    if not stations:
        logger.error("No NDBC station data retrieved")
        sys.exit(1)

    logger.info(f"Fetched {len(stations)}/{len(NDBC_STATIONS)} NDBC stations")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "stations.json"
    out_path.write_text(json.dumps(stations))

    # Update meta
    meta_path = OUTPUT_DIR.parent / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["ndbc_updated"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))

    logger.info(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
