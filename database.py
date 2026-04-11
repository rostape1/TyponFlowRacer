import asyncio
import os
import aiosqlite
import logging
from datetime import datetime, timezone

from config import DB_PATH, OWN_MMSI

logger = logging.getLogger(__name__)

# Serialize all DB writes through a single lock
_db_lock = asyncio.Lock()


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS vessels (
            mmsi INTEGER PRIMARY KEY,
            name TEXT,
            ship_type INTEGER,
            ship_category TEXT,
            destination TEXT,
            length INTEGER,
            beam INTEGER,
            is_own_vessel INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mmsi INTEGER NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            sog REAL,
            cog REAL,
            heading INTEGER,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (mmsi) REFERENCES vessels(mmsi)
        )
    """)

    await db.execute("CREATE INDEX IF NOT EXISTS idx_positions_mmsi ON positions(mmsi)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_positions_timestamp ON positions(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_positions_mmsi_ts ON positions(mmsi, timestamp)")

    await db.commit()
    logger.info(f"Database initialized at {db_path}")

    # Start background commit task
    asyncio.create_task(_periodic_commit(db))

    return db


async def _periodic_commit(db: aiosqlite.Connection):
    """Commit every 2 seconds to batch writes."""
    while True:
        await asyncio.sleep(2)
        try:
            async with _db_lock:
                await db.commit()
        except Exception:
            pass


async def upsert_vessel(db: aiosqlite.Connection, data: dict):
    now = datetime.now(timezone.utc).isoformat()
    mmsi = data["mmsi"]

    async with _db_lock:
        cursor = await db.execute("SELECT mmsi FROM vessels WHERE mmsi = ?", (mmsi,))
        existing = await cursor.fetchone()

        if existing:
            updates = []
            values = []
            for field in ("name", "ship_type", "ship_category", "destination", "length", "beam"):
                val = data.get("shipname" if field == "name" else field)
                if val is not None:
                    updates.append(f"{field} = ?")
                    values.append(val)
            updates.append("last_seen = ?")
            values.append(now)
            updates.append("is_own_vessel = ?")
            values.append(1 if data.get("is_own_vessel") else 0)
            values.append(mmsi)

            await db.execute(f"UPDATE vessels SET {', '.join(updates)} WHERE mmsi = ?", values)
        else:
            await db.execute(
                """INSERT INTO vessels (mmsi, name, ship_type, ship_category, destination, length, beam, is_own_vessel, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mmsi,
                    data.get("shipname"),
                    data.get("ship_type"),
                    data.get("ship_category"),
                    data.get("destination"),
                    data.get("length"),
                    data.get("beam"),
                    1 if data.get("is_own_vessel") else 0,
                    now,
                    now,
                ),
            )


async def insert_position(db: aiosqlite.Connection, data: dict):
    if "lat" not in data or "lon" not in data:
        return
    # Normalize timestamp to SQLite-compatible format (YYYY-MM-DD HH:MM:SS)
    ts = data.get("timestamp", datetime.now(timezone.utc).isoformat())
    try:
        dt = datetime.fromisoformat(ts)
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with _db_lock:
        await db.execute(
            """INSERT INTO positions (mmsi, lat, lon, sog, cog, heading, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["mmsi"],
                data["lat"],
                data["lon"],
                data.get("sog"),
                data.get("cog"),
                data.get("heading"),
                ts,
            ),
        )


async def get_all_vessels(db: aiosqlite.Connection) -> list[dict]:
    """Get all vessels with their latest position."""
    async with _db_lock:
        cursor = await db.execute("""
            SELECT v.*, p.lat, p.lon, p.sog, p.cog, p.heading, p.timestamp as pos_timestamp
            FROM vessels v
            LEFT JOIN positions p ON v.mmsi = p.mmsi
                AND p.timestamp = (SELECT MAX(timestamp) FROM positions WHERE mmsi = v.mmsi)
            ORDER BY v.is_own_vessel DESC, v.last_seen DESC
        """)
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_vessel_detail(db: aiosqlite.Connection, mmsi: int) -> dict | None:
    async with _db_lock:
        cursor = await db.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,))
        row = await cursor.fetchone()
        if not row:
            return None
        vessel = dict(row)

        cursor = await db.execute(
            "SELECT * FROM positions WHERE mmsi = ? ORDER BY timestamp DESC LIMIT 1", (mmsi,)
        )
        pos = await cursor.fetchone()
    if pos:
        vessel.update({"lat": pos["lat"], "lon": pos["lon"], "sog": pos["sog"], "cog": pos["cog"], "heading": pos["heading"]})

    return vessel


async def get_vessel_track(db: aiosqlite.Connection, mmsi: int, hours: float | None = 2.0) -> list[dict]:
    async with _db_lock:
        if hours is not None:
            cursor = await db.execute(
                """SELECT lat, lon, sog, cog, heading, timestamp FROM positions
                   WHERE mmsi = ? AND timestamp >= datetime('now', ?)
                   ORDER BY timestamp ASC""",
                (mmsi, f"-{hours} hours"),
            )
        else:
            cursor = await db.execute(
                "SELECT lat, lon, sog, cog, heading, timestamp FROM positions WHERE mmsi = ? ORDER BY timestamp ASC",
                (mmsi,),
            )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_avg_speed(db: aiosqlite.Connection, mmsi: int, minutes: int = 30) -> float | None:
    async with _db_lock:
        cursor = await db.execute(
            """SELECT AVG(sog) as avg_sog FROM positions
               WHERE mmsi = ? AND sog IS NOT NULL AND timestamp >= datetime('now', ?)""",
            (mmsi, f"-{minutes} minutes"),
        )
        row = await cursor.fetchone()
    if row and row["avg_sog"] is not None:
        return round(row["avg_sog"], 1)
    return None


async def get_stats(db: aiosqlite.Connection) -> dict:
    async with _db_lock:
        vessel_count = (await (await db.execute("SELECT COUNT(*) FROM vessels")).fetchone())[0]
        position_count = (await (await db.execute("SELECT COUNT(*) FROM positions")).fetchone())[0]
    own = await get_vessel_detail(db, OWN_MMSI)
    return {
        "vessel_count": vessel_count,
        "position_count": position_count,
        "own_vessel": own,
    }
