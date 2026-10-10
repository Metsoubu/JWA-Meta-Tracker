"""Builds for snapshots saved before the tracker recorded them.

Older versions only stored which creatures each team used. The level,
enhancement, stat boosts and omega training of those teams are filled in here
from the saved raw copy of the snapshot, or, if there is none, from the same
public file again. A build is attached only to the team whose published record
has exactly the same fingerprint, and nothing already stored is ever changed.
Snapshots imported by this version are marked when they are saved, so they are
never downloaded again just to look for builds.
"""
from __future__ import annotations

import gzip
import logging
import sqlite3
from datetime import datetime

from . import config, db, ingest, net
from .sources import jwa_dashboard_feed
from .sources.base import SnapshotRef, SourceFormatError, SourceSnapshot

log = logging.getLogger(__name__)


def snapshots_to_check(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    """Snapshots of `source` saved by an older version that did not record builds."""
    return conn.execute(
        """
        SELECT s.id, s.source_snapshot_id, s.captured_at FROM snapshots s
        LEFT JOIN snapshot_builds b ON b.snapshot_id = s.id
        WHERE s.source = ? AND s.is_example = 0 AND b.snapshot_id IS NULL
        ORDER BY s.captured_at
        """,
        (source,),
    ).fetchall()


def _load(source, row: sqlite3.Row) -> SourceSnapshot:
    snapshot_id = row["source_snapshot_id"]
    if source.name == jwa_dashboard_feed.SOURCE_NAME:
        saved = config.raw_dir() / source.name / f"{snapshot_id}.json.gz"
        if saved.exists():
            try:
                return jwa_dashboard_feed.parse_snapshot(gzip.decompress(saved.read_bytes()), snapshot_id)
            except (OSError, EOFError, SourceFormatError) as exc:
                log.warning("Saved copy of %s is unreadable (%s); downloading it again.", snapshot_id, exc)
    snapshot, _raw = source.fetch_snapshot(
        SnapshotRef(id=snapshot_id, captured_at=db.parse_iso(row["captured_at"]))
    )
    return snapshot


def _mark(conn: sqlite3.Connection, snapshot_id: int, status: str, now: datetime) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO snapshot_builds (snapshot_id, status, checked_at) VALUES (?, ?, ?)",
        (snapshot_id, status, db.iso(now)),
    )


def backfill(conn: sqlite3.Connection, sources, now: datetime | None = None, limit: int = 200) -> int:
    """Attach builds to older snapshots. Returns how many snapshots got builds."""
    now = now or db.utcnow()
    done = 0
    for source in sources:
        for row in snapshots_to_check(conn, source.name)[:limit]:
            try:
                snapshot = _load(source, row)
            except net.FetchError as exc:
                log.warning("Could not get builds for %s: %s", row["source_snapshot_id"], exc)
                if not exc.transient:
                    with db.transaction(conn):
                        _mark(conn, row["id"], "unavailable", now)
                continue  # a temporary problem: try again next update
            except (SourceFormatError, ValueError, KeyError) as exc:
                log.warning("Could not get builds for %s: %s", row["source_snapshot_id"], exc)
                with db.transaction(conn):
                    _mark(conn, row["id"], "unavailable", now)
                continue
            team_ids = {
                r["fingerprint"]: r["id"]
                for r in conn.execute(
                    "SELECT id, fingerprint FROM teams WHERE snapshot_id = ? AND fingerprint IS NOT NULL", (row["id"],)
                )
            }
            with db.transaction(conn):
                if conn.execute("SELECT 1 FROM snapshot_builds WHERE snapshot_id = ?", (row["id"],)).fetchone():
                    continue  # another update got here first
                stored = 0
                for prepared in ingest.prepare(snapshot).teams:
                    team_id = team_ids.get(prepared.team.fingerprint)
                    if team_id is not None:
                        stored += db.insert_builds(conn, team_id, prepared.team.members)
                _mark(conn, row["id"], "filled" if stored else "unavailable", now)
            done += 1 if stored else 0
    return done
