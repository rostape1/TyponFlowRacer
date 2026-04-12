import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

# Curated SF Bay tide stations — only those with NOAA predictions available
STATIONS = {
    # Golden Gate / SF
    "9414290": {"name": "San Francisco (Golden Gate)", "lat": 37.8063, "lon": -122.4659},
    # Central Bay
    "9414750": {"name": "Alameda", "lat": 37.7720, "lon": -122.3003},
    "9414764": {"name": "Oakland Inner Harbor", "lat": 37.7950, "lon": -122.2820},
    "9414816": {"name": "Berkeley", "lat": 37.8650, "lon": -122.3070},
    # Angel Island / Tiburon / Sausalito area (nearest working station)
    "9414874": {"name": "Corte Madera Creek", "lat": 37.9433, "lon": -122.5130},
    # East Bay
    "9414688": {"name": "San Leandro Marina", "lat": 37.6950, "lon": -122.1920},
    # South Bay / Redwood City area
    "9414523": {"name": "Redwood City", "lat": 37.5068, "lon": -122.2119},
    "9414458": {"name": "San Mateo Bridge (West)", "lat": 37.5800, "lon": -122.2530},
    "9414509": {"name": "Dumbarton Bridge", "lat": 37.5067, "lon": -122.1150},
    # Half Moon Bay (outer coast reference)
    "9414131": {"name": "Half Moon Bay", "lat": 37.5025, "lon": -122.4822},
    # North Bay / Richmond
    "9414863": {"name": "Richmond (Chevron Pier)", "lat": 37.9283, "lon": -122.4000},
    "9415056": {"name": "Pinole Point", "lat": 38.0150, "lon": -122.3630},
    # Carquinez / Suisun
    "9415102": {"name": "Martinez", "lat": 38.0346, "lon": -122.1252},
    "9415144": {"name": "Port Chicago", "lat": 38.0560, "lon": -122.0395},
}

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# Cache: "station_date" → { predictions: [...], fetched_at: datetime }
_cache: dict[str, dict] = {}

# Offline cache directory
OFFLINE_DIR = Path(__file__).parent / "static" / "data" / "tides"


def _build_url(station_id: str, begin_date: str = None, end_date: str = None) -> str:
    if begin_date and end_date:
        date_part = f"begin_date={begin_date}&end_date={end_date}"
    else:
        date_part = "date=today"
    return (
        f"{NOAA_BASE}?{date_part}&station={station_id}"
        f"&product=predictions&datum=MLLW&units=english"
        f"&time_zone=gmt&format=json&interval=6"
    )


def _fetch_url(url: str) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError):
        return None


async def fetch_predictions(station_id: str, begin_date: str = None, end_date: str = None) -> list[dict] | None:
    """Fetch tide height predictions from NOAA. Returns list of {t, v} dicts.

    If begin_date/end_date not provided, fetches today's data.
    Dates in YYYYMMDD format.
    """
    cache_key = f"{station_id}_{begin_date or 'today'}_{end_date or ''}"
    cached = _cache.get(cache_key)
    if cached and (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() < 6 * 3600:
        return cached["predictions"]

    # Check offline cache
    offline_file = OFFLINE_DIR / f"{station_id}.json"
    if offline_file.exists():
        try:
            data = json.loads(offline_file.read_text())
            # Check if offline data covers the requested date range
            match_date = begin_date[:4] + "-" + begin_date[4:6] + "-" + begin_date[6:8] if begin_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if any(match_date in p.get("t", "") for p in data):
                _cache[cache_key] = {"predictions": data, "fetched_at": datetime.now(timezone.utc)}
                return data
        except Exception:
            pass

    url = _build_url(station_id, begin_date, end_date)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _fetch_url, url)
        if result is None:
            return None

        parsed = json.loads(result)
        predictions = parsed.get("predictions")
        if predictions:
            _cache[cache_key] = {"predictions": predictions, "fetched_at": datetime.now(timezone.utc)}
            return predictions
        return None
    except Exception as e:
        logger.debug(f"Failed to fetch tide heights for {station_id}: {e}")
        return None


def _parse_predictions(predictions: list[dict]) -> list[tuple[datetime, float]]:
    """Parse raw NOAA predictions into (datetime, height_ft) tuples."""
    result = []
    for p in predictions:
        try:
            t = datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            v = float(p["v"])
            result.append((t, v))
        except (KeyError, ValueError):
            continue
    result.sort(key=lambda x: x[0])
    return result


def interpolate_height(predictions: list[dict], target: datetime) -> float | None:
    """Interpolate tide height at a specific time."""
    parsed = _parse_predictions(predictions)
    if not parsed:
        return None

    for i in range(len(parsed) - 1):
        t0, v0 = parsed[i]
        t1, v1 = parsed[i + 1]
        if t0 <= target <= t1:
            total = (t1 - t0).total_seconds()
            if total == 0:
                return v0
            frac = (target - t0).total_seconds() / total
            return round(v0 + frac * (v1 - v0), 2)

    if target <= parsed[0][0]:
        return parsed[0][1]
    return parsed[-1][1]


def find_next_extreme(predictions: list[dict], target: datetime) -> dict | None:
    """Find the next high or low tide after target time."""
    parsed = _parse_predictions(predictions)
    if len(parsed) < 3:
        return None

    for i in range(1, len(parsed) - 1):
        t, v = parsed[i]
        if t <= target:
            continue

        v_prev = parsed[i - 1][1]
        v_next = parsed[i + 1][1]

        if v >= v_prev and v >= v_next and v > v_prev:
            return {"type": "High", "time": t.strftime("%Y-%m-%d %H:%M"), "height_ft": round(v, 2)}

        if v <= v_prev and v <= v_next and v < v_prev:
            return {"type": "Low", "time": t.strftime("%Y-%m-%d %H:%M"), "height_ft": round(v, 2)}

    return None


async def _get_station_tide(station_id: str, info: dict, eval_time: datetime) -> dict:
    """Get tide data for a single station. Always returns a dict (height_ft may be None)."""
    begin = eval_time.strftime("%Y%m%d")
    end = (eval_time + timedelta(days=1)).strftime("%Y%m%d")

    base = {"station_id": station_id, "name": info["name"],
            "lat": info["lat"], "lon": info["lon"],
            "height_ft": None, "next_extreme": None}

    preds = await fetch_predictions(station_id, begin_date=begin, end_date=end)
    if not preds:
        # Retry once after a short pause
        await asyncio.sleep(1)
        preds = await fetch_predictions(station_id, begin_date=begin, end_date=end)
    if not preds:
        return base

    height = interpolate_height(preds, eval_time)
    extreme = find_next_extreme(preds, eval_time)

    base["height_ft"] = height
    base["next_extreme"] = extreme
    return base


async def get_all_tide_heights(target_time: datetime | None = None) -> list[dict]:
    """Get tide height data for all stations."""
    eval_time = target_time or datetime.now(timezone.utc)

    tasks = [
        _get_station_tide(sid, info, eval_time)
        for sid, info in STATIONS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return [r for r in results if isinstance(r, dict)]


async def save_offline_cache():
    """Download tide predictions for all stations (today + tomorrow) and save to disk."""
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    now = datetime.now(timezone.utc)
    begin = now.strftime("%Y%m%d")
    end = (now + timedelta(days=2)).strftime("%Y%m%d")

    for station_id in STATIONS:
        predictions = await fetch_predictions(station_id, begin_date=begin, end_date=end)
        if predictions:
            offline_file = OFFLINE_DIR / f"{station_id}.json"
            offline_file.write_text(json.dumps(predictions, indent=2))
            saved += 1
            logger.info(f"Cached tides for {station_id} ({len(predictions)} predictions)")
    return saved
