"""Central configuration: paths, thresholds, and fixed settings.

Data lives outside the project folder (in %LOCALAPPDATA%) by default because the
project sits inside OneDrive, and cloud-sync tools can lock or conflict with a
live SQLite database. Set the JWA_TRACKER_DATA_DIR environment variable to move it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import __version__

APP_NAME = "JWA Meta Tracker"
APP_ID = "jwa-meta-tracker"
APP_VERSION = __version__

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
WEB_DIR = PROJECT_DIR / "web"
BUNDLED_DATA_DIR = PACKAGE_DIR / "data"
SILHOUETTE_DIR = WEB_DIR / "img" / "silhouettes"
ENTRY_SCRIPT = PROJECT_DIR / "tracker.py"


def data_dir() -> Path:
    """Folder holding the database, logs, cache and raw downloads."""
    override = os.environ.get("JWA_TRACKER_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".jwa-meta-tracker"


def db_path() -> Path:
    return data_dir() / "tracker.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def log_file() -> Path:
    return log_dir() / "collector.log"


def cache_dir() -> Path:
    return data_dir() / "cache"


def raw_dir() -> Path:
    return data_dir() / "raw"


def lock_file() -> Path:
    return data_dir() / "collector.lock"


# --- Network -------------------------------------------------------------------
USER_AGENT = f"JWA-Meta-Tracker/{APP_VERSION} (private personal dashboard)"
HTTP_TIMEOUT_SECONDS = 30
HTTP_ATTEMPTS = 4
HTTP_BACKOFF_SECONDS = (5, 20, 60)

# --- Dashboard server ----------------------------------------------------------
HOST = "127.0.0.1"  # never bind to 0.0.0.0: the dashboard must stay private
DEFAULT_PORT = 8765
PORT_SEARCH_RANGE = 10

# --- Scheduling ----------------------------------------------------------------
TASK_NAME = "JWA Meta Tracker - Auto Update"
# Collection "slots" in local time, 3 hours apart. Windows Task Scheduler wakes
# the collector every hour; it only does work when a slot has passed without a
# successful check. This gives a 3-hour cadence plus hourly retry/catch-up.
SCHEDULE_SLOTS_LOCAL = ("00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00")
STALE_AFTER_HOURS = 30
VERY_STALE_AFTER_HOURS = 72
ROSTER_REFRESH_HOURS = 24

# --- Statistics ----------------------------------------------------------------
TEAM_SIZE = 8
MAX_RANK = 500
# (tier, minimum usage percentage). Checked top-down with exact integer maths.
TIERS = (("S", 80), ("A", 60), ("B", 40), ("C", 20), ("D", 0))
TIER_LABELS = {
    "S": "80–100%",
    "A": "60–79.99%",
    "B": "40–59.99%",
    "C": "20–39.99%",
    "D": "Below 20%",
}
# key -> (lowest rank, highest rank, label, group)
RANK_FILTERS = {
    "top50": (1, 50, "Top 50", "top"),
    "top100": (1, 100, "Top 100", "top"),
    "top250": (1, 250, "Top 250", "top"),
    "top500": (1, 500, "Top 500", "top"),
    "r1-50": (1, 50, "#1–50", "bracket"),
    "r51-100": (51, 100, "#51–100", "bracket"),
    "r101-250": (101, 250, "#101–250", "bracket"),
    "r251-500": (251, 500, "#251–500", "bracket"),
}
DEFAULT_RANK_FILTER = "top500"
TREND_POINTS = 12
