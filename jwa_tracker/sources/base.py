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
class Build:
    """How a player built one creature on their team, as the source publishes it.

    `boosts` and `omega` are (stat name, points) pairs. `boosts` is None when the
    source did not publish them (an empty tuple means "no boosts"); `omega` is None
    for creatures without omega training.
    """

    level: float | None = None
    enhancement: int | None = None
    boosts: tuple[tuple[str, float], ...] | None = None
    omega: tuple[tuple[str, float], ...] | None = None


@dataclass(frozen=True)
class SourceCreature:
    source_id: str
    display_name: str
    rarity: str | None = None
    build: Build | None = None


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
    # Which parts of a build count on this leaderboard (tournaments may fix every level
    # or switch stat boosts off): {"levels", "boosts", "enhancements": bool,
    # "standardized_level": int | None}. None = the source does not say.
    build_rules: dict | None = None
