"""
Muse Cloud Server Configuration
Loads settings from .env file, then environment variables, then defaults.
"""

import os
from pathlib import Path

# ── Load .env file if present ─────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE)
    except ImportError:
        # python-dotenv not installed — manually parse .env
        with open(_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = val

# ── Server ───────────────────────────────────────────────────────────
HOST = os.getenv("MUSE_HOST", "0.0.0.0")
PORT = int(os.getenv("MUSE_PORT", "8000"))
LOG_LEVEL = os.getenv("MUSE_LOG_LEVEL", "info")

# ── MySQL Database ────────────────────────────────────────────────────
MYSQL_HOST = os.getenv("MUSE_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MUSE_MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MUSE_MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MUSE_MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MUSE_MYSQL_DATABASE", "muse_cloud")
MYSQL_POOL_SIZE = int(os.getenv("MUSE_MYSQL_POOL_SIZE", "5"))

# ── File Storage ──────────────────────────────────────────────────────
STORAGE_DIR = os.getenv("MUSE_STORAGE_DIR", "./muse_sessions")
MAX_SESSION_DURATION_SECONDS = int(os.getenv("MUSE_MAX_SESSION_DURATION", "7200"))

# ── WebSocket ─────────────────────────────────────────────────────────
WS_HEARTBEAT_TIMEOUT = int(os.getenv("MUSE_WS_HEARTBEAT_TIMEOUT", "60"))
WS_MAX_SESSIONS = int(os.getenv("MUSE_WS_MAX_SESSIONS", "10"))

# ── Database Batching ─────────────────────────────────────────────────
DB_FLUSH_INTERVAL = float(os.getenv("MUSE_DB_FLUSH_INTERVAL", "1.0"))


def print_config(logger):
    """Print a startup configuration summary."""
    logger.info("─" * 50)
    logger.info("Configuration:")
    logger.info("  Server:   %s:%d  (log: %s)", HOST, PORT, LOG_LEVEL)
    logger.info("  Storage:  %s", os.path.abspath(STORAGE_DIR))
    logger.info("  MySQL:    %s:%d/%s  (user: %s, pool: %d)",
                MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE,
                MYSQL_USER, MYSQL_POOL_SIZE)
    if MYSQL_PASSWORD:
        logger.info("            password: ***configured***")
    else:
        logger.info("            password: (empty — will run FILE-ONLY mode)")
    logger.info("  Sessions: max %d concurrent, %ds heartbeat timeout",
                WS_MAX_SESSIONS, WS_HEARTBEAT_TIMEOUT)
    logger.info("─" * 50)
