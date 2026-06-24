"""
MySQL storage backend using pymysql (sync) via asyncio.to_thread.
Chosen over aiomysql because TencentDB CynosDB has compatibility issues
with async MySQL drivers (connection hangs / 2013 errors).
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

import pymysql
import pymysql.cursors

from .base import StorageBackend, SessionInfo

logger = logging.getLogger(__name__)


class MySQLBackend(StorageBackend):
    """MySQL backend using pymysql via thread pool for reliable cloud DB access."""

    def __init__(self,
                 host: str = "127.0.0.1",
                 port: int = 3306,
                 user: str = "root",
                 password: str = "",
                 database: str = "muse_cloud",
                 pool_size: int = 5):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self._connected = False

    # ── Connection ─────────────────────────────────────────────

    async def connect(self) -> None:
        """Verify connectivity + auto-create database and tables."""
        # 1. Ensure database exists
        await self._ensure_database()

        # 2. Test connectivity
        await self._run_sync(lambda cur: cur.execute("SELECT 1"))

        # 3. Ensure tables exist
        await self._ensure_tables()

        self._connected = True
        logger.info("MySQL ready: %s:%d/%s", self.host, self.port, self.database)

    async def close(self) -> None:
        self._connected = False

    def _get_conn(self):
        """Create a new pymysql connection."""
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset='utf8mb4',
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            autocommit=True,
        )

    async def _run_sync(self, fn):
        """Run a sync pymysql operation in a thread."""
        def _do():
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    result = fn(cur)
                return result
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    # ── Database creation ─────────────────────────────────────

    async def _ensure_database(self) -> None:
        """Create database if it doesn't exist."""
        def _do():
            conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                charset='utf8mb4',
                connect_timeout=10,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
            finally:
                conn.close()
        await asyncio.to_thread(_do)

    async def _ensure_tables(self) -> None:
        ddl = [
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id CHAR(12) PRIMARY KEY,
                device_address VARCHAR(17) NOT NULL DEFAULT '',
                device_name VARCHAR(64) NOT NULL DEFAULT '',
                preset VARCHAR(16) NOT NULL DEFAULT 'p1034',
                started_at DATETIME(3) NOT NULL,
                ended_at DATETIME(3) DEFAULT NULL,
                raw_file_path VARCHAR(512) DEFAULT '',
                raw_file_size_bytes BIGINT DEFAULT 0,
                total_packets INT DEFAULT 0,
                firmware_version VARCHAR(16) DEFAULT '',
                battery_start_pct INT DEFAULT NULL,
                tags JSON DEFAULT NULL,
                INDEX idx_started_at (started_at),
                INDEX idx_device_address (device_address)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS heart_rate_samples (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                session_id CHAR(12) NOT NULL,
                time DATETIME(3) NOT NULL,
                bpm REAL NOT NULL,
                INDEX idx_session_time (session_id, time)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS eeg_band_power (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                session_id CHAR(12) NOT NULL,
                time DATETIME(3) NOT NULL,
                channel VARCHAR(8) NOT NULL,
                delta REAL DEFAULT 0,
                theta REAL DEFAULT 0,
                alpha REAL DEFAULT 0,
                beta REAL DEFAULT 0,
                gamma REAL DEFAULT 0,
                INDEX idx_session_channel_time (session_id, channel, time)
            ) ENGINE=InnoDB""",

            """CREATE TABLE IF NOT EXISTS session_stats (
                session_id CHAR(12) PRIMARY KEY,
                eeg_packets INT DEFAULT 0,
                ppg_packets INT DEFAULT 0,
                imu_packets INT DEFAULT 0,
                decode_errors INT DEFAULT 0,
                updated_at DATETIME DEFAULT NOW()
            ) ENGINE=InnoDB""",
        ]
        await self._run_sync(lambda cur: [cur.execute(s) for s in ddl])

    # ── Session CRUD ──────────────────────────────────────────

    async def create_session(self, session: SessionInfo) -> None:
        def _do(cur):
            cur.execute(
                """INSERT INTO sessions
                   (session_id, device_address, device_name, preset, started_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (session.session_id, session.device_address,
                 session.device_name, session.preset, session.started_at)
            )
            cur.execute(
                "INSERT INTO session_stats (session_id) VALUES (%s)",
                (session.session_id,)
            )
        await self._run_sync(_do)

    async def close_session(self, session_id: str, ended_at: datetime,
                            total_packets: int, raw_file_path: str,
                            raw_file_size_bytes: int) -> None:
        await self._run_sync(lambda cur: cur.execute(
            """UPDATE sessions SET
                 ended_at=%s, total_packets=%s,
                 raw_file_path=%s, raw_file_size_bytes=%s
               WHERE session_id=%s""",
            (ended_at, total_packets, raw_file_path,
             raw_file_size_bytes, session_id)
        ))

    async def write_heart_rate(self, session_id: str,
                                time: datetime, bpm: float) -> None:
        await self._run_sync(lambda cur: cur.execute(
            "INSERT INTO heart_rate_samples (session_id, time, bpm) "
            "VALUES (%s, %s, %s)",
            (session_id, time, bpm)
        ))

    async def write_band_power(self, session_id: str, time: datetime,
                                channel: str,
                                delta: float, theta: float,
                                alpha: float, beta: float,
                                gamma: float) -> None:
        await self._run_sync(lambda cur: cur.execute(
            """INSERT INTO eeg_band_power
               (session_id, time, channel, delta, theta, alpha, beta, gamma)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (session_id, time, channel, delta, theta, alpha, beta, gamma)
        ))

    async def flush_buffers(self, session_id: str) -> None:
        pass  # pymysql writes are immediate

    # ── Query ──────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> Optional[Dict]:
        def _do(cur):
            cur.execute(
                """SELECT session_id, device_address, device_name, preset,
                          started_at, ended_at, total_packets, raw_file_path,
                          raw_file_size_bytes
                   FROM sessions WHERE session_id=%s""",
                (session_id,)
            )
            return cur.fetchone()
        row = await self._run_sync(_do)
        return _row_to_dict(row, _SESSION_KEYS) if row else None

    async def list_sessions(self, limit: int = 50,
                            offset: int = 0) -> List[Dict]:
        def _do(cur):
            cur.execute(
                """SELECT session_id, device_address, device_name, preset,
                          started_at, ended_at, total_packets, raw_file_path,
                          raw_file_size_bytes
                   FROM sessions ORDER BY started_at DESC LIMIT %s OFFSET %s""",
                (limit, offset)
            )
            return cur.fetchall()
        rows = await self._run_sync(_do)
        return [_row_to_dict(r, _SESSION_KEYS) for r in rows]

    async def get_heart_rate(self, session_id: str,
                              from_time: Optional[datetime] = None,
                              to_time: Optional[datetime] = None
                              ) -> List[tuple]:
        def _do(cur):
            sql = "SELECT time, bpm FROM heart_rate_samples WHERE session_id=%s"
            args = [session_id]
            if from_time:
                sql += " AND time >= %s"; args.append(from_time)
            if to_time:
                sql += " AND time <= %s"; args.append(to_time)
            sql += " ORDER BY time ASC"
            cur.execute(sql, args)
            return cur.fetchall()
        return await self._run_sync(_do)

    async def get_band_power(self, session_id: str,
                              channel: Optional[str] = None,
                              from_time: Optional[datetime] = None,
                              to_time: Optional[datetime] = None
                              ) -> List[tuple]:
        def _do(cur):
            sql = """SELECT time, channel, delta, theta, alpha, beta, gamma
                     FROM eeg_band_power WHERE session_id=%s"""
            args = [session_id]
            if channel:
                sql += " AND channel=%s"; args.append(channel)
            if from_time:
                sql += " AND time >= %s"; args.append(from_time)
            if to_time:
                sql += " AND time <= %s"; args.append(to_time)
            sql += " ORDER BY time ASC"
            cur.execute(sql, args)
            return cur.fetchall()
        return await self._run_sync(_do)


_SESSION_KEYS = [
    "session_id", "device_address", "device_name", "preset",
    "started_at", "ended_at", "total_packets", "raw_file_path",
    "raw_file_size_bytes"
]


def _row_to_dict(row: tuple, keys: list) -> Dict:
    result = {}
    for i, k in enumerate(keys):
        if i >= len(row):
            break
        val = row[i]
        if isinstance(val, datetime):
            val = val.isoformat()
        result[k] = val
    return result
