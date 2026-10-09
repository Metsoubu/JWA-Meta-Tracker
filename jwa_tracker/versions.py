"""Game-version timeline.

Each collection asks Apple's public iTunes Lookup API which Jurassic World Alive
version is live. A new major.minor version (e.g. v3.24) is added to the timeline
starting at that build's App Store release date. Snapshots are matched to a
version by their capture time, so the timeline can be corrected later without
touching the (read-only) snapshots themselves.
"""
from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_right
from datetime import datetime
from typing import Any, Callable

from . import config, db, net

APP_STORE_ID = "1231085864"
LOOKUP_URL = f"https://itunes.apple.com/lookup?id={APP_STORE_ID}"
KNOWN_VERSIONS_FILE = config.BUNDLED_DATA_DIR / "known_versions.json"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)")


def minor_version(build: str) -> str | None:
    match = _VERSION_RE.match(build or "")
    return f"v{int(match.group(1))}.{int(match.group(2))}" if match else None


def fetch_app_store_version(fetch_json: Callable[..., Any] = net.fetch_json) -> tuple[str, datetime]:
    doc = fetch_json(LOOKUP_URL, attempts=2)
    try:
        result = doc["results"][0]
        build = str(result["version"])
        released = db.parse_iso(result["currentVersionReleaseDate"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"App Store lookup returned an unexpected answer ({exc})") from exc
    if not minor_version(build) or released is None:
        raise ValueError(f"App Store lookup returned an unusable version {build!r}")
    return build, released


def seed_known_versions(conn: sqlite3.Connection, now: datetime) -> None:
    with open(KNOWN_VERSIONS_FILE, encoding="utf-8") as fh:
        seeds = json.load(fh)["versions"]
    for v in seeds:
        conn.execute(
            "INSERT OR IGNORE INTO game_versions (version, starts_at, basis, first_build, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (v["version"], v["starts_at"], v["basis"], v.get("first_build"), db.iso(now)),
        )


def record_build(conn: sqlite3.Connection, build: str, released: datetime, now: datetime) -> str | None:
    """Add the build's minor version to the timeline if it is new. Returns the new version."""
    version = minor_version(build)
    if not version:
        return None
    exists = conn.execute("SELECT 1 FROM game_versions WHERE version = ?", (version,)).fetchone()
    db.set_meta(conn, "app_store_build", build)
    db.set_meta(conn, "app_store_checked_at", db.iso(now))
    if exists:
        return None
    conn.execute(
        "INSERT INTO game_versions (version, starts_at, basis, first_build, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (version, db.iso(released), f"App Store release date of build {build}", build, db.iso(now)),
    )
    return version


class VersionTimeline:
    def __init__(self, rows: list[dict[str, Any]]):
        rows = sorted(rows, key=lambda r: r["starts_at"])
        self._starts = [db.parse_iso(r["starts_at"]) for r in rows]
        self._rows = rows

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "VersionTimeline":
        return cls(db.game_versions(conn))

    def version_at(self, moment: datetime | None) -> str | None:
        if moment is None:
            return None
        index = bisect_right(self._starts, moment) - 1
        return self._rows[index]["version"] if index >= 0 else None

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)
