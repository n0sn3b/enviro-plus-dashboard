"""
SQLite database for sensor data storage.

Schema:
  readings table — one row per recording interval (default 60s)
  Columns: timestamp, temperature, humidity, pressure, light,
           no2_raw, co_raw, nh3_raw, ldr,
           no2_ppm, co_ppm, nh3_ppm

Features:
  - Auto-creates tables on first use; migrates existing DBs
  - Daily high/low queries (resets at midnight)
  - Historical range queries for the web dashboard
  - Calibration offsets applied on read (not stored raw)
"""

import sqlite3
from datetime import datetime, timedelta, timezone


class SensorDatabase:
    """SQLite-backed sensor data store."""

    CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            temperature REAL,
            humidity REAL,
            pressure REAL,
            light REAL,
            no2_raw INTEGER,
            co_raw INTEGER,
            nh3_raw INTEGER,
            ldr INTEGER,
            no2_ppm REAL,
            co_ppm REAL,
            nh3_ppm REAL
        );
    """

    CREATE_INDEX = """
        CREATE INDEX IF NOT EXISTS idx_timestamp ON readings(timestamp);
    """

    # Columns added to pre-existing databases (migration).
    MIGRATE_COLUMNS = {
        "no2_ppm": "REAL",
        "co_ppm": "REAL",
        "nh3_ppm": "REAL",
    }

    INSERT = """
        INSERT INTO readings (timestamp, temperature, humidity, pressure,
                              light, no2_raw, co_raw, nh3_raw, ldr,
                              no2_ppm, co_ppm, nh3_ppm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._ensure_schema()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent read/write
        return conn

    def _ensure_schema(self):
        with self._get_conn() as conn:
            conn.execute(self.CREATE_TABLE)
            self._migrate(conn)
            conn.execute(self.CREATE_INDEX)
            conn.commit()

    def _migrate(self, conn):
        """Add any missing columns to an existing readings table."""
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(readings)").fetchall()
        }
        for col, coltype in self.MIGRATE_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {coltype}")

    def insert_reading(self, data):
        """
        Insert a single sensor reading.

        Args:
            data: dict with keys matching the readings columns.
                  Missing/None values are stored as NULL.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(self.INSERT, (
                ts,
                data.get("temperature"),
                data.get("humidity"),
                data.get("pressure"),
                data.get("light"),
                data.get("no2_raw"),
                data.get("co_raw"),
                data.get("nh3_raw"),
                data.get("ldr"),
                data.get("no2_ppm"),
                data.get("co_ppm"),
                data.get("nh3_ppm"),
            ))
            conn.commit()

    def daily_high_low(self, now=None):
        """
        Get daily high/low for each numeric sensor since midnight UTC.

        Returns dict:
        {
            "temp_high": float, "temp_low": float,
            "hum_high": float, "hum_low": float,
            "pressure_high": float, "pressure_low": float,
            "light_high": float, "light_low": float,
            "no2_raw_high": int, "no2_raw_low": int,
            "co_raw_high": int, "co_raw_low": int,
            "nh3_raw_high": int, "nh3_raw_low": int,
        }
        """
        if now is None:
            now = datetime.now(timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.isoformat()

        query = """
            SELECT
                MAX(temperature) as temp_high, MIN(temperature) as temp_low,
                MAX(humidity) as hum_high, MIN(humidity) as hum_low,
                MAX(pressure) as pressure_high, MIN(pressure) as pressure_low,
                MAX(light) as light_high, MIN(light) as light_low,
                MAX(no2_raw) as no2_raw_high, MIN(no2_raw) as no2_raw_low,
                MAX(co_raw) as co_raw_high, MIN(co_raw) as co_raw_low,
                MAX(nh3_raw) as nh3_raw_high, MIN(nh3_raw) as nh3_raw_low
            FROM readings
            WHERE timestamp >= ?
        """
        with self._get_conn() as conn:
            row = conn.execute(query, (since,)).fetchone()
            if row:
                return dict(row)
            return {}

    def latest_reading(self):
        """Get the most recent reading."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                return dict(row)
            return None

    def historical_readings(self, hours=24, limit=None):
        """
        Get readings going back `hours` hours.

        Args:
            hours: How many hours of history to retrieve.
            limit: Max rows to return (default: enough for 1 reading/min).

        Returns:
            List of dicts, ordered by timestamp ascending.
        """
        if limit is None:
            limit = hours * 60 + 100

        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        query = """
            SELECT * FROM readings
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            LIMIT ?
        """
        with self._get_conn() as conn:
            rows = conn.execute(query, (since, limit)).fetchall()
            return [dict(r) for r in rows]

    def readings_for_chart(self, sensor, hours=24, limit=None):
        """
        Get timestamp + single sensor column for charting.

        Returns:
            List of {"timestamp": str, "value": float} dicts.
        """
        col_map = {
            "temperature": "temperature",
            "humidity": "humidity",
            "pressure": "pressure",
            "light": "light",
            "no2_raw": "no2_raw",
            "co_raw": "co_raw",
            "nh3_raw": "nh3_raw",
            "ldr": "ldr",
            "no2_ppm": "no2_ppm",
            "co_ppm": "co_ppm",
            "nh3_ppm": "nh3_ppm",
        }
        col = col_map.get(sensor)
        if not col:
            return []

        if limit is None:
            limit = hours * 60 + 100

        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        query = f"""
            SELECT timestamp, {col} as value FROM readings
            WHERE timestamp >= ? AND {col} IS NOT NULL
            ORDER BY timestamp ASC
            LIMIT ?
        """
        with self._get_conn() as conn:
            rows = conn.execute(query, (since, limit)).fetchall()
            return [dict(r) for r in rows]

    def reading_count(self):
        """Total number of readings in the database."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM readings").fetchone()
            return row["cnt"] if row else 0

    def purge_old(self, keep_days=90):
        """
        Delete readings older than `keep_days` days to prevent DB bloat.

        Returns number of rows deleted.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat()
        with self._get_conn() as conn:
            result = conn.execute(
                "DELETE FROM readings WHERE timestamp < ?", (cutoff,)
            )
            count = result.rowcount
            conn.commit()
            return count

    def export_csv(self, hours=24):
        """
        Export readings as CSV string.

        Returns:
            CSV string with header row.
        """
        rows = self.historical_readings(hours=hours, limit=100000)
        if not rows:
            return ""

        headers = list(rows[0].keys())
        lines = [",".join(headers)]
        for row in rows:
            values = []
            for h in headers:
                v = row.get(h)
                values.append("" if v is None else str(v))
            lines.append(",".join(values))
        return "\n".join(lines)
