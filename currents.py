import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

# SF Bay current stations
STATIONS = {
    "SFB1201": {"name": "Golden Gate Bridge", "lat": 37.8117, "lon": -122.4717},
    "SFB1203": {"name": "Alcatraz (North)", "lat": 37.8317, "lon": -122.4217},
    "SFB1204": {"name": "Alcatraz (South)", "lat": 37.8183, "lon": -122.4200},
    "SFB1205": {"name": "Angel Island (East)", "lat": 37.8633, "lon": -122.4217},
    "SFB1206": {"name": "Raccoon Strait", "lat": 37.8567, "lon": -122.4467},
    "SFB1211": {"name": "Bay Bridge", "lat": 37.8033, "lon": -122.3633},
}

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# Cache: station_id → { predictions: [...], fetched_at: datetime }
_cache: dict[str, dict] = {}

# Offline cache directory
OFFLINE_DIR = Path(__file__).parent / "static" / "data" / "currents"


def _build_url(station_id: str, date: str = "today") -> str:
    return (
        f"{NOAA_BASE}?date={date}&station={station_id}"
        f"&product=currents_predictions&units=english"
        f"&time_zone=gmt&format=json&interval=6"
    )


async def fetch_predictions(station_id: str, date_str: str = "today") -> list[dict] | None:
    """Fetch current predictions from NOAA for a station. Returns list of prediction dicts.

    date_str: NOAA date param — "today" or "YYYYMMDD".
    """
    cache_key = f"{station_id}_{date_str}"

    # Check memory cache first
    cached = _cache.get(cache_key)
    if cached and (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() < 6 * 3600:
        return cached["predictions"]

    # Check offline cache (for any date)
    offline_file = OFFLINE_DIR / f"{station_id}.json"
    if offline_file.exists():
        try:
            data = json.loads(offline_file.read_text())
            # Convert YYYYMMDD to YYYY-MM-DD for matching against prediction Time field
            if date_str == "today":
                match_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            else:
                match_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            if any(match_date in p.get("Time", "") for p in data):
                _cache[cache_key] = {"predictions": data, "fetched_at": datetime.now(timezone.utc)}
                return data
        except Exception:
            pass

    # Fetch from NOAA API
    url = _build_url(station_id, date=date_str)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _fetch_url, url)
        if result is None:
            return None

        parsed = json.loads(result)

        # Handle different response formats
        predictions = None
        if "current_predictions" in parsed:
            predictions = parsed["current_predictions"].get("cp", [])
        elif "predictions" in parsed:
            predictions = parsed["predictions"]

        if predictions:
            _cache[cache_key] = {"predictions": predictions, "fetched_at": datetime.now(timezone.utc)}
            return predictions

        return None

    except Exception as e:
        logger.debug(f"Failed to fetch currents for {station_id}: {e}")
        return None


def _fetch_url(url: str) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError):
        return None


def interpolate_current(predictions: list[dict], now: datetime = None) -> dict | None:
    """Interpolate predictions to get current speed and direction at a given time."""
    if not predictions:
        return None

    now = now or datetime.now(timezone.utc)

    # Parse prediction times and find the surrounding pair
    parsed = []
    for p in predictions:
        try:
            t = datetime.strptime(p["Time"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            vel = float(p["Velocity_Major"])
            flood_dir = float(p.get("meanFloodDir", 0))
            ebb_dir = float(p.get("meanEbbDir", 180))
            parsed.append({"time": t, "velocity": vel, "flood_dir": flood_dir, "ebb_dir": ebb_dir})
        except (KeyError, ValueError):
            continue

    if not parsed:
        return None

    # Sort by time
    parsed.sort(key=lambda x: x["time"])

    # Find the two predictions surrounding 'now'
    before = None
    after = None
    for i, p in enumerate(parsed):
        if p["time"] <= now:
            before = p
            if i + 1 < len(parsed):
                after = parsed[i + 1]
        elif before is None:
            # 'now' is before all predictions
            return _format_prediction(parsed[0])
        else:
            break

    if before is None:
        return None

    if after is None:
        return _format_prediction(before)

    # Linear interpolation
    total = (after["time"] - before["time"]).total_seconds()
    if total == 0:
        return _format_prediction(before)

    frac = (now - before["time"]).total_seconds() / total
    vel = before["velocity"] + frac * (after["velocity"] - before["velocity"])

    # Use flood or ebb direction based on velocity sign
    direction = before["flood_dir"] if vel >= 0 else before["ebb_dir"]

    return {
        "speed": round(abs(vel), 2),
        "direction": direction,
        "type": "flood" if vel >= 0 else "ebb",
        "velocity": round(vel, 2),
    }


def _format_prediction(p: dict) -> dict:
    vel = p["velocity"]
    return {
        "speed": round(abs(vel), 2),
        "direction": p["flood_dir"] if vel >= 0 else p["ebb_dir"],
        "type": "flood" if vel >= 0 else "ebb",
        "velocity": round(vel, 2),
    }


async def get_all_currents(target_time: datetime | None = None) -> list[dict]:
    """Fetch current data for all SF Bay stations.

    target_time: UTC datetime to evaluate predictions at (None = now).
    """
    # Determine which date(s) to fetch predictions for
    eval_time = target_time or datetime.now(timezone.utc)
    date_str = eval_time.strftime("%Y%m%d")

    results = []
    for station_id, info in STATIONS.items():
        predictions = await fetch_predictions(station_id, date_str=date_str)
        if predictions:
            current = interpolate_current(predictions, now=eval_time)
            if current:
                results.append({
                    "station_id": station_id,
                    "name": info["name"],
                    "lat": info["lat"],
                    "lon": info["lon"],
                    **current,
                })
    return results


async def save_offline_cache():
    """Download predictions for all stations (today + tomorrow) and save to disk for offline use."""
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    now = datetime.now(timezone.utc)
    dates = [
        now.strftime("%Y%m%d"),
        (now + timedelta(days=1)).strftime("%Y%m%d"),
    ]
    for station_id in STATIONS:
        all_predictions = []
        for date_str in dates:
            predictions = await fetch_predictions(station_id, date_str=date_str)
            if predictions:
                all_predictions.extend(predictions)
        if all_predictions:
            # Deduplicate by Time field
            seen = set()
            unique = []
            for p in all_predictions:
                t = p.get("Time", "")
                if t not in seen:
                    seen.add(t)
                    unique.append(p)
            offline_file = OFFLINE_DIR / f"{station_id}.json"
            offline_file.write_text(json.dumps(unique, indent=2))
            saved += 1
            logger.info(f"Cached currents for {station_id} ({len(unique)} predictions)")
    return saved
