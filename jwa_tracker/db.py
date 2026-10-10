"""SQLite storage.

Historical snapshots, their teams and team members are append-only: database
triggers reject any UPDATE or DELETE, so a bug or a failed update can never
overwrite history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS creatures (
    key           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    rarity        TEXT,
    class         TEXT,
    hybrid_type   TEXT,
    added_version TEXT,
    roster_source TEXT NOT NULL,
    in_roster     INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_creatures (
    source             TEXT NOT NULL,
    source_creature_id TEXT NOT NULL,
    display_name       TEXT,
    rarity             TEXT,
    creature_key       TEXT NOT NULL,
    match_method       TEXT NOT NULL,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    PRIMARY KEY (source, source_creature_id)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id                INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    trigger           TEXT NOT NULL,
    status            TEXT NOT NULL,
    new_snapshots     INTEGER NOT NULL DEFAULT 0,
    failed_snapshots  INTEGER NOT NULL DEFAULT 0,
    message           TEXT,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                   INTEGER PRIMARY KEY,
    source               TEXT NOT NULL,
    source_snapshot_id   TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    leaderboard_id       TEXT,
    leaderboard_name     TEXT,
    captured_at          TEXT NOT NULL,
    imported_at          TEXT NOT NULL,
    collection_run_id    INTEGER,
    rank_coverage_max    INTEGER NOT NULL,
    declared_team_count  INTEGER,
    teams_total          INTEGER NOT NULL,
    teams_valid          INTEGER NOT NULL,
    teams_invalid        INTEGER NOT NULL,
    duplicates_dropped   INTEGER NOT NULL,
    content_sha256       TEXT NOT NULL,
    is_example           INTEGER NOT NULL DEFAULT 0,
    notes                TEXT,
    UNIQUE (source, source_snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_kind_time ON snapshots (kind, captured_at);

CREATE TABLE IF NOT EXISTS teams (
    id             INTEGER PRIMARY KEY,
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots (id),
    rank_min       INTEGER,
    rank_max       INTEGER,
    trophies       INTEGER,
    player_key     TEXT,
    is_valid       INTEGER NOT NULL,
    invalid_reason TEXT,
    fingerprint    TEXT
);
CREATE INDEX IF NOT EXISTS idx_teams_snapshot ON teams (snapshot_id);

CREATE TABLE IF NOT EXISTS team_members (
    team_id            INTEGER NOT NULL REFERENCES teams (id),
    slot               INTEGER NOT NULL,
    source_creature_id TEXT NOT NULL,
    PRIMARY KEY (team_id, slot)
);

-- How each creature on a team was built. Older snapshots get these rows later
-- (builds.py), so this table is separate from team_members; still append-only.
CREATE TABLE IF NOT EXISTS member_builds (
    team_id     INTEGER NOT NULL REFERENCES teams (id),
    slot        INTEGER NOT NULL,
    level       REAL,
    enhancement INTEGER,
    boosts      TEXT,   -- JSON {"Attack": 19, ...}; NULL when the source did not publish boosts
    omega       TEXT,   -- JSON omega training points; NULL for creatures without omega training
    PRIMARY KEY (team_id, slot)
);
-- Whether a snapshot's builds were looked at: 'recorded' when it was imported,
-- 'filled' or 'unavailable' after builds.py looked at an older snapshot.
CREATE TABLE IF NOT EXISTS snapshot_builds (
    snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots (id),
    status      TEXT NOT NULL,
    checked_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_snapshots (
    source             TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    reason             TEXT NOT NULL,
    rejected_at        TEXT NOT NULL,
    PRIMARY KEY (source, source_snapshot_id)
);

CREATE TABLE IF NOT EXISTS game_versions (
    version     TEXT PRIMARY KEY,
    starts_at   TEXT NOT NULL,
    basis       TEXT NOT NULL,
    first_build TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'historical snapshots are read-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'historical snapshots are read-only'); END;
CREATE TRIGGER IF NOT EXISTS teams_no_update BEFORE UPDATE ON teams
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
CREATE TRIGGER IF NOT EXISTS teams_no_delete BEFORE DELETE ON teams
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
CREATE TRIGGER IF NOT EXISTS team_members_no_update BEFORE UPDATE ON team_members
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
CREATE TRIGGER IF NOT EXISTS team_members_no_delete BEFORE DELETE ON team_members
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
CREATE TRIGGER IF NOT EXISTS member_builds_no_update BEFORE UPDATE ON member_builds
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
CREATE TRIGGER IF NOT EXISTS member_builds_no_delete BEFORE DELETE ON member_builds
BEGIN SELECT RAISE(ABORT, 'historical teams are read-only'); END;
"""

SUCCESS_STATUSES = ("success", "no_new_data", "partial")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def connect(path: Path | str | None = None, initialize: bool = True) -> sqlite3.Connection:
    path = Path(path) if path else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if initialize:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class transaction:
    """`with transaction(conn):` - BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on error."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


# --- meta ------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- collection runs -------------------------------------------------------------

def start_run(conn: sqlite3.Connection, trigger: str, now: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO collection_runs (started_at, trigger, status) VALUES (?, ?, 'running')",
        (iso(now or utcnow()), trigger),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    new_snapshots: int = 0,
    failed_snapshots: int = 0,
    message: str = "",
    error: str = "",
    now: datetime | None = None,
) -> None:
    conn.execute(
        "UPDATE collection_runs SET finished_at = ?, status = ?, new_snapshots = ?, "
        "failed_snapshots = ?, message = ?, error = ? WHERE id = ?",
        (iso(now or utcnow()), status, new_snapshots, failed_snapshots, message, error or None, run_id),
    )


def mark_interrupted_runs(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Runs left 'running' by a crash or shutdown. Only call while holding the collector lock."""
    cur = conn.execute(
        "UPDATE collection_runs SET status = 'interrupted', finished_at = ?, "
        "error = COALESCE(error, 'The update stopped unexpectedly (computer shut down or process ended).') "
        "WHERE status = 'running'",
        (iso(now or utcnow()),),
    )
    return cur.rowcount


def recent_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def last_run(conn: sqlite3.Connection, statuses: Iterable[str] | None = None) -> dict[str, Any] | None:
    if statuses:
        statuses = tuple(statuses)
        marks = ",".join("?" * len(statuses))
        row = conn.execute(
            f"SELECT * FROM collection_runs WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1", statuses
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM collection_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def consecutive_failures(conn: sqlite3.Connection) -> int:
    count = 0
    for row in conn.execute(
        "SELECT status FROM collection_runs WHERE status != 'running' ORDER BY id DESC LIMIT 100"
    ):
        if row["status"] in SUCCESS_STATUSES:
            break
        count += 1
    return count


# --- snapshots -------------------------------------------------------------------

def known_snapshot_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    return {
        r["source_snapshot_id"]
        for r in conn.execute("SELECT source_snapshot_id FROM snapshots WHERE source = ?", (source,))
    }


def rejected_snapshot_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    return {
        r["source_snapshot_id"]
        for r in conn.execute("SELECT source_snapshot_id FROM rejected_snapshots WHERE source = ?", (source,))
    }


def reject_snapshot(conn: sqlite3.Connection, source: str, snapshot_id: str, reason: str, now: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO rejected_snapshots (source, source_snapshot_id, reason, rejected_at) "
        "VALUES (?, ?, ?, ?)",
        (source, snapshot_id, reason, iso(now)),
    )


def rejected_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM rejected_snapshots ORDER BY rejected_at DESC")]


def list_snapshots(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE kind = ? ORDER BY captured_at DESC, id DESC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM snapshots ORDER BY captured_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return dict(row) if row else None


def latest_snapshot(conn: sqlite3.Connection, kind: str | None = None) -> dict[str, Any] | None:
    snaps = list_snapshots(conn, kind)
    return snaps[0] if snaps else None


def load_team_rows(conn: sqlite3.Connection, snapshot_ids: list[int]) -> list[sqlite3.Row]:
    """Valid teams of the given snapshots, one row per (team, creature)."""
    if not snapshot_ids:
        return []
    out: list[sqlite3.Row] = []
    chunk = 400
    for start in range(0, len(snapshot_ids), chunk):
        ids = snapshot_ids[start:start + chunk]
        marks = ",".join("?" * len(ids))
        out.extend(
            conn.execute(
                f"""
                SELECT t.id AS team_id, t.snapshot_id, t.rank_min, t.rank_max,
                       sc.creature_key
                FROM teams t
                JOIN snapshots s ON s.id = t.snapshot_id
                JOIN team_members m ON m.team_id = t.id
                JOIN source_creatures sc
                  ON sc.source = s.source AND sc.source_creature_id = m.source_creature_id
                WHERE t.snapshot_id IN ({marks}) AND t.is_valid = 1
                """,
                ids,
            ).fetchall()
        )
    return out


def insert_builds(conn: sqlite3.Connection, team_id: int, members: Iterable[Any]) -> int:
    """Store the builds of a team's members (SourceCreature list, slot = list position)."""
    def as_json(points):
        return None if points is None else json.dumps(dict(points), sort_keys=True)

    rows = [
        (team_id, slot, m.build.level, m.build.enhancement, as_json(m.build.boosts), as_json(m.build.omega))
        for slot, m in enumerate(members)
        if m.source_id and m.build is not None
    ]
    conn.executemany(
        "INSERT INTO member_builds (team_id, slot, level, enhancement, boosts, omega) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def load_team_members(conn: sqlite3.Connection, snapshot_ids: list[int]) -> list[sqlite3.Row]:
    """Valid teams of the given snapshots, one row per member with its build (if known), in slot order."""
    out: list[sqlite3.Row] = []
    chunk = 400
    for start in range(0, len(snapshot_ids), chunk):
        ids = snapshot_ids[start:start + chunk]
        marks = ",".join("?" * len(ids))
        out.extend(
            conn.execute(
                f"""
                SELECT t.id AS team_id, t.snapshot_id, t.rank_min, t.rank_max, m.slot, sc.creature_key,
                       b.level, b.enhancement, b.boosts, b.omega
                FROM teams t
                JOIN snapshots s ON s.id = t.snapshot_id
                JOIN team_members m ON m.team_id = t.id
                JOIN source_creatures sc
                  ON sc.source = s.source AND sc.source_creature_id = m.source_creature_id
                LEFT JOIN member_builds b ON b.team_id = m.team_id AND b.slot = m.slot
                WHERE t.snapshot_id IN ({marks}) AND t.is_valid = 1
                ORDER BY t.snapshot_id, t.id, m.slot
                """,
                ids,
            ).fetchall()
        )
    return out


def invalid_team_count(conn: sqlite3.Connection, snapshot_ids: list[int]) -> int:
    if not snapshot_ids:
        return 0
    marks = ",".join("?" * len(snapshot_ids))
    return conn.execute(
        f"SELECT COUNT(*) FROM teams WHERE snapshot_id IN ({marks}) AND is_valid = 0", snapshot_ids
    ).fetchone()[0]


# --- creatures -------------------------------------------------------------------

def all_creatures(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM creatures")}


def creature_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM creatures WHERE in_roster = 1").fetchone()[0]


# --- game versions ---------------------------------------------------------------

def game_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM game_versions ORDER BY starts_at")]
