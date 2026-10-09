"""Import a leaderboard from a CSV file (for data you are permitted to use).

This is the extension point for top-500 data: no permitted automatic source of
ranks 101-500 exists today, but if you obtain such data with permission (for
example an export someone shares with you), save it as CSV and import it:

    python tracker.py import-csv my_export.csv --captured-at 2026-10-08T20:00

Columns (header row required, names are case-insensitive):
    rank        - required, a number (or a band like 11-20)
    trophies    - optional
    player      - optional (used only to remove duplicate rows; never displayed)
    creature1 ... creature8 - required, creature names as shown in the game
"""
from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime
from pathlib import Path

from .base import SourceCreature, SourceFormatError, SourceSnapshot, SourceTeam

SOURCE_NAME = "csv-import"
_BAND = re.compile(r"^\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*$")


def parse_csv(path: Path, captured_at: datetime, kind: str = "arena", label: str = "") -> SourceSnapshot:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SourceFormatError("the CSV file has no header row")
        fields = {f.strip().lower(): f for f in reader.fieldnames}
        creature_cols = [fields[f"creature{i}"] for i in range(1, 9) if f"creature{i}" in fields]
        if "rank" not in fields or len(creature_cols) != 8:
            raise SourceFormatError("the CSV needs a 'rank' column and columns creature1 ... creature8")
        teams: list[SourceTeam] = []
        for line_no, row in enumerate(reader, start=2):
            match = _BAND.match(row.get(fields["rank"]) or "")
            lo = int(match.group(1)) if match else None
            hi = int(match.group(2) or match.group(1)) if match else None
            trophies_raw = (row.get(fields["trophies"]) if "trophies" in fields else "") or ""
            trophies = int(trophies_raw.replace(",", "").strip()) if trophies_raw.strip().replace(",", "").isdigit() else None
            player = (row.get(fields["player"]) if "player" in fields else "") or ""
            members = []
            for col in creature_cols:
                name = (row.get(col) or "").strip()
                if name:
                    members.append(SourceCreature(source_id=re.sub(r"[^a-z0-9]", "", name.lower()), display_name=name))
            fingerprint = hashlib.sha256(f"{lo}|{hi}|{sorted(m.source_id for m in members)}".encode()).hexdigest()
            teams.append(
                SourceTeam(
                    rank_min=lo, rank_max=hi, members=members, trophies=trophies,
                    player_key=hashlib.sha256(player.strip().lower().encode()).hexdigest() if player.strip() else None,
                    fingerprint=fingerprint,
                )
            )
    if not teams:
        raise SourceFormatError("the CSV file contains no rows")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return SourceSnapshot(
        source=SOURCE_NAME,
        source_snapshot_id=f"{path.stem}-{digest}",
        captured_at=captured_at,
        kind=kind,
        leaderboard_id="",
        leaderboard_name=label or f"Imported from {path.name}",
        teams=teams,
        declared_team_count=len(teams),
        fingerprint_identifies_player=False,
    )
