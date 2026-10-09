"""Source-independent data shapes shared by every adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class SourceFormatError(Exception):
    """The source returned data we cannot safely interpret. Nothing is imported."""


@dataclass(frozen=True)
class SnapshotRef:
    """A snapshot the source says it has, before downloading it."""

    id: str
    captured_at: datetime
    label: str = ""
    leaderboard_id: str = ""


@dataclass(frozen=True)
class SourceCreature:
    source_id: str
    display_name: str
    rarity: str | None = None


@dataclass
class SourceTeam:
    rank_min: int | None
    rank_max: int | None
    members: list[SourceCreature]
    trophies: int | None = None
    player_key: str | None = None
    fingerprint: str = ""


@dataclass
class SourceSnapshot:
    source: str
    source_snapshot_id: str
    captured_at: datetime
    kind: str  # "arena" | "tournament" | "other"
    leaderboard_id: str
    leaderboard_name: str
    teams: list[SourceTeam]
    declared_team_count: int | None = None
    # True when `fingerprint` includes per-creature detail rich enough that two
    # different players would practically never share it (levels, boosts, ...).
    fingerprint_identifies_player: bool = False
    notes: list[str] = field(default_factory=list)
