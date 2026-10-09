"""Validate, de-duplicate and store a SourceSnapshot (source independent).

Rules
- A team is *valid* only if it has a usable rank and exactly eight different,
  identified creatures. Invalid teams are stored (for transparency) but never
  counted in usage statistics.
- Duplicates: if the source gives player identifiers, a player is counted once
  (best rank kept). Otherwise identical team records inside the same rank band
  are removed when the source's records are detailed enough to identify a
  player, or when a band holds more teams than it has ranks.
- A snapshot is stored in a single transaction: either all of it, or nothing.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import config, db, roster
from .sources.base import SourceSnapshot, SourceTeam

log = logging.getLogger(__name__)


@dataclass
class PreparedTeam:
    team: SourceTeam
    is_valid: bool
    invalid_reason: str | None


@dataclass
class PreparedSnapshot:
    snapshot: SourceSnapshot
    teams: list[PreparedTeam]
    duplicates_dropped: int
    rank_coverage_max: int
    notes: list[str] = field(default_factory=list)

    @property
    def valid_count(self) -> int:
        return sum(1 for t in self.teams if t.is_valid)

    @property
    def invalid_count(self) -> int:
        return sum(1 for t in self.teams if not t.is_valid)


def validate_team(team: SourceTeam) -> str | None:
    """Return why a team is invalid, or None if it is valid."""
    if team.rank_min is None or team.rank_max is None:
        return "missing rank"
    if team.rank_min < 1 or team.rank_max < team.rank_min:
        return "invalid rank"
    if any(not m.source_id for m in team.members):
        return "creature without an id"
    if len(team.members) != config.TEAM_SIZE:
        return f"has {len(team.members)} creatures (expected {config.TEAM_SIZE})"
    if len({m.source_id for m in team.members}) != len(team.members):
        return "same creature listed twice"
    return None


def dedupe_teams(snapshot: SourceSnapshot) -> tuple[list[SourceTeam], int, list[str]]:
    teams = sorted(
        snapshot.teams,
        key=lambda t: (t.rank_min if t.rank_min is not None else 10**9, t.rank_max or 10**9),
    )
    notes: list[str] = []
    kept: list[SourceTeam] = []
    dropped = 0

    seen_players: set[str] = set()
    for team in teams:
        if team.player_key:
            key = team.player_key.strip().lower()
            if key in seen_players:
                dropped += 1
                continue
            seen_players.add(key)
        kept.append(team)

    bands: dict[tuple[int | None, int | None], list[SourceTeam]] = {}
    for team in kept:
        bands.setdefault((team.rank_min, team.rank_max), []).append(team)
    result: list[SourceTeam] = []
    for (lo, hi), members in bands.items():
        capacity = (hi - lo + 1) if lo is not None and hi is not None and hi >= lo else None
        over = capacity is not None and len(members) > capacity
        if snapshot.fingerprint_identifies_player or over:
            unique: list[SourceTeam] = []
            fingerprints: set[str] = set()
            for team in members:
                if team.player_key is None and team.fingerprint and team.fingerprint in fingerprints:
                    dropped += 1
                    continue
                fingerprints.add(team.fingerprint)
                unique.append(team)
            members = unique
        if capacity is not None and len(members) > capacity:
            notes.append(f"Rank band {lo}-{hi} lists {len(members)} teams for {capacity} ranks.")
        result.extend(members)
    result.sort(key=lambda t: (t.rank_min if t.rank_min is not None else 10**9, t.rank_max or 10**9))
    if dropped:
        notes.append(f"Removed {dropped} duplicate team record(s).")
    return result, dropped, notes


def prepare(snapshot: SourceSnapshot) -> PreparedSnapshot:
    teams, dropped, notes = dedupe_teams(snapshot)
    prepared = []
    for team in teams:
        reason = validate_team(team)
        prepared.append(PreparedTeam(team, reason is None, reason))
    valid_ranks = [p.team.rank_max for p in prepared if p.is_valid and p.team.rank_max]
    coverage = min(max(valid_ranks), config.MAX_RANK) if valid_ranks else 0
    invalid = sum(1 for p in prepared if not p.is_valid)
    if invalid:
        notes.append(f"{invalid} team(s) were incomplete or malformed and are excluded from statistics.")
    return PreparedSnapshot(snapshot, prepared, dropped, coverage, list(snapshot.notes) + notes)


def content_hash(prepared: PreparedSnapshot) -> str:
    body = [
        [t.team.rank_min, t.team.rank_max, t.team.trophies, [m.source_id for m in t.team.members]]
        for t in prepared.teams
    ]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class DuplicateSnapshot(Exception):
    pass


def store(
    conn: sqlite3.Connection,
    prepared: PreparedSnapshot,
    *,
    now: datetime,
    run_id: int | None = None,
    raw: bytes | None = None,
    is_example: bool = False,
) -> int:
    """Insert one snapshot atomically. Returns the new snapshot id."""
    snap = prepared.snapshot
    if prepared.valid_count == 0:
        raise ValueError(f"snapshot {snap.source_snapshot_id} contains no valid teams; not imported")
    resolver = roster.resolver_for(conn)
    creatures = {}
    for t in prepared.teams:
        for m in t.team.members:
            if m.source_id:
                creatures[m.source_id] = (m.source_id, m.display_name, m.rarity)

    with db.transaction(conn):
        exists = conn.execute(
            "SELECT id FROM snapshots WHERE source = ? AND source_snapshot_id = ?",
            (snap.source, snap.source_snapshot_id),
        ).fetchone()
        if exists:
            raise DuplicateSnapshot(snap.source_snapshot_id)
        roster.register_source_creatures(conn, snap.source, creatures.values(), now, resolver)
        cur = conn.execute(
            """
            INSERT INTO snapshots (source, source_snapshot_id, kind, leaderboard_id, leaderboard_name,
                                   captured_at, imported_at, collection_run_id, rank_coverage_max,
                                   declared_team_count, teams_total, teams_valid, teams_invalid,
                                   duplicates_dropped, content_sha256, is_example, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.source, snap.source_snapshot_id, snap.kind, snap.leaderboard_id, snap.leaderboard_name,
                db.iso(snap.captured_at), db.iso(now), run_id, prepared.rank_coverage_max,
                snap.declared_team_count, len(prepared.teams), prepared.valid_count, prepared.invalid_count,
                prepared.duplicates_dropped, content_hash(prepared), 1 if is_example else 0,
                "\n".join(prepared.notes) or None,
            ),
        )
        snapshot_id = int(cur.lastrowid)
        for t in prepared.teams:
            team_cur = conn.execute(
                "INSERT INTO teams (snapshot_id, rank_min, rank_max, trophies, player_key, is_valid, "
                "invalid_reason, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, t.team.rank_min, t.team.rank_max, t.team.trophies, t.team.player_key,
                 1 if t.is_valid else 0, t.invalid_reason, t.team.fingerprint or None),
            )
            team_id = team_cur.lastrowid
            conn.executemany(
                "INSERT INTO team_members (team_id, slot, source_creature_id) VALUES (?, ?, ?)",
                [(team_id, slot, m.source_id) for slot, m in enumerate(t.team.members) if m.source_id],
            )

    if raw is not None:
        try:
            folder = config.raw_dir() / snap.source
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{snap.source_snapshot_id}.json.gz").write_bytes(gzip.compress(raw))
        except OSError as exc:  # the archive copy is a convenience, never fatal
            log.warning("Could not archive raw snapshot %s: %s", snap.source_snapshot_id, exc)
    return snapshot_id
