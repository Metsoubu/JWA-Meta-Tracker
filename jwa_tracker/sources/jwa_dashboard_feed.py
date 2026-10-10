"""Adapter for the public "jwa-dashboard" GitHub feed.

Repository: https://github.com/Lullatsch/jwa-dashboard (unofficial community project)

The repository publishes timestamped JSON snapshots of Jurassic World Alive
leaderboards (the monthly arena "Seasonal League" ladder and weekly tournaments).
Each snapshot holds the eight-creature teams of the top 100 players, anonymised
into rank bands of ten (1-10, 11-20, ...). It does not publish player names or
per-player trophy counts.

We only read files that the maintainer publishes on GitHub. Nothing here talks
to Ludia's game servers.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .. import net
from .base import Build, SnapshotRef, SourceCreature, SourceFormatError, SourceSnapshot, SourceTeam

SOURCE_NAME = "jwa-dashboard-feed"
REPOSITORY = "Lullatsch/jwa-dashboard"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}/main/data"
TESTED_SCHEMAS = ("jwa-dashboard.public.v2",)
SCHEMA_FAMILY = "jwa-dashboard.public."

_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_BAND_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$")


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise SourceFormatError(f"missing or invalid timestamp {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceFormatError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_band(team: dict[str, Any]) -> tuple[int | None, int | None]:
    band = team.get("leaderboard_rank_band")
    if isinstance(band, str):
        match = _BAND_RE.match(band)
        if match:
            return int(match.group(1)), int(match.group(2))
        if band.strip().isdigit():
            rank = int(band.strip())
            return rank, rank
    rank = team.get("rank")
    if isinstance(rank, int):
        return rank, rank
    return None, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _stat_points(value: Any) -> tuple[tuple[str, float], ...] | None:
    if not isinstance(value, dict):
        return None
    points = [(k.strip(), _number(v)) for k, v in value.items() if isinstance(k, str) and k.strip()]
    return tuple(sorted((k, v) for k, v in points if v is not None))


def parse_build(creature: dict[str, Any]) -> Build | None:
    """Level, enhancement, stat boosts and omega training of one creature on a team."""
    level = _number(creature.get("level"))
    enhancement = _number(creature.get("enhancement_level"))
    boosts = _stat_points(creature.get("stat_boosts"))
    omega = _stat_points(creature.get("omega_training_points"))
    if level is None and enhancement is None and boosts is None and omega is None:
        return None
    return Build(level, int(enhancement) if enhancement is not None else None, boosts, omega or None)


def _kind(event: dict[str, Any], name: str) -> str:
    if event.get("is_arena") is True:
        return "arena"
    if name.startswith("[Tournament]"):
        return "tournament"
    return "other"


def parse_snapshot(raw: bytes, snapshot_id: str) -> SourceSnapshot:
    """Parse one dashboard-data.json file. Raises SourceFormatError if unusable."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceFormatError(f"snapshot {snapshot_id} is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise SourceFormatError(f"snapshot {snapshot_id} has an unexpected layout")

    notes: list[str] = []
    schema = doc.get("schema_version")
    if schema not in TESTED_SCHEMAS:
        if not isinstance(schema, str) or not schema.startswith(SCHEMA_FAMILY):
            raise SourceFormatError(
                f"snapshot {snapshot_id} uses an unknown format ({schema!r}); refusing to guess"
            )
        notes.append(f"Upstream format {schema} is newer than the tested {TESTED_SCHEMAS[-1]}.")

    teams_raw = doc.get("anonymized_teams")
    if not isinstance(teams_raw, list):
        raise SourceFormatError(f"snapshot {snapshot_id} has no team list")
    event = doc.get("event") if isinstance(doc.get("event"), dict) else {}
    name = str(event.get("name") or doc.get("dashboard_title") or "").strip()
    captured_at = parse_timestamp(doc.get("captured_at"))

    teams: list[SourceTeam] = []
    for team in teams_raw:
        if not isinstance(team, dict):
            teams.append(SourceTeam(rank_min=None, rank_max=None, members=[], fingerprint=""))
            continue
        rank_min, rank_max = _parse_band(team)
        members = []
        for creature in team.get("creatures") or []:
            if not isinstance(creature, dict):
                continue
            members.append(
                SourceCreature(
                    source_id=str(creature.get("creature_id") or "").strip(),
                    display_name=str(creature.get("display_name") or ""),
                    rarity=(str(creature["rarity"]) if creature.get("rarity") else None),
                    build=parse_build(creature),
                )
            )
        fingerprint = hashlib.sha256(
            json.dumps(team, sort_keys=True, ensure_ascii=True).encode("ascii")
        ).hexdigest()
        teams.append(SourceTeam(rank_min=rank_min, rank_max=rank_max, members=members, fingerprint=fingerprint))

    declared = doc.get("captured_team_count")
    declared = declared if isinstance(declared, int) else None
    if declared is not None and declared != len(teams):
        notes.append(f"Upstream declared {declared} teams but published {len(teams)}.")

    return SourceSnapshot(
        source=SOURCE_NAME,
        source_snapshot_id=snapshot_id,
        captured_at=captured_at,
        kind=_kind(event, name),
        leaderboard_id=str(doc.get("leaderboard_id") or ""),
        leaderboard_name=name,
        teams=teams,
        declared_team_count=declared,
        fingerprint_identifies_player=True,
        notes=notes,
    )


def parse_run_index(doc: Any) -> list[SnapshotRef]:
    """Parse runs.json, the upstream list of published snapshots."""
    if not isinstance(doc, list):
        raise SourceFormatError("runs.json is not a list")
    refs: list[SnapshotRef] = []
    for entry in doc:
        if not isinstance(entry, dict):
            continue
        folder = entry.get("snapshot_folder")
        if not isinstance(folder, str) or not _FOLDER_RE.match(folder):
            continue  # ignore anything that does not look like a snapshot folder
        try:
            captured_at = parse_timestamp(entry.get("captured_at"))
        except SourceFormatError:
            continue
        refs.append(
            SnapshotRef(
                id=folder,
                captured_at=captured_at,
                label=str(entry.get("label") or ""),
                leaderboard_id=str(entry.get("leaderboard_id") or ""),
            )
        )
    if doc and not refs:
        raise SourceFormatError("runs.json contained no recognisable snapshots")
    refs.sort(key=lambda ref: ref.captured_at)
    return refs


class JwaDashboardFeed:
    name = SOURCE_NAME
    label = "jwa-dashboard public GitHub feed"
    url = REPOSITORY_URL
    max_rank = 100

    def __init__(
        self,
        fetch: Callable[..., bytes] = net.fetch,
        fetch_json: Callable[..., Any] = net.fetch_json,
        base_url: str = RAW_BASE,
    ):
        self._fetch = fetch
        self._fetch_json = fetch_json
        self._base = base_url.rstrip("/")

    def list_snapshots(self) -> list[SnapshotRef]:
        return parse_run_index(self._fetch_json(f"{self._base}/runs.json"))

    def fetch_snapshot(self, ref: SnapshotRef) -> tuple[SourceSnapshot, bytes]:
        if not _FOLDER_RE.match(ref.id):
            raise SourceFormatError(f"refusing unexpected snapshot id {ref.id!r}")
        raw = self._fetch(f"{self._base}/{ref.id}/dashboard-data.json")
        return parse_snapshot(raw, ref.id), raw
