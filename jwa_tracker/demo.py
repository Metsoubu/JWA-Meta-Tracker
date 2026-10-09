"""EXAMPLE DATA generator - for testing the 500-player views only.

Builds a separate, throw-away database full of randomly generated teams so the
Top 250 / Top 500 filters can be exercised. It never touches the real database,
and every snapshot is flagged is_example=1, which makes the dashboard show a
large "EXAMPLE DATA - NOT REAL" banner.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, ingest, roster, versions
from .sources.base import SourceCreature, SourceSnapshot, SourceTeam

SOURCE_NAME = "example"


def demo_db_path() -> Path:
    return config.cache_dir() / "example-data.db"


def build_demo_db(path: Path | None = None, snapshots: int = 8, players: int = 500, seed: int = 7) -> Path:
    path = path or demo_db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    rng = random.Random(seed)
    conn = db.connect(path)
    now = datetime.now(timezone.utc)
    roster.ensure_roster(conn, now)
    with db.transaction(conn):
        versions.seed_known_versions(conn, now)
    pool = [c for c in roster.load_seed_roster() if c["rarity"] in ("apex", "unique", "omega", "legendary")]
    rng.shuffle(pool)
    weights = [1.0 / (i + 1) ** 0.9 for i in range(len(pool))]
    for n in range(snapshots):
        captured = now - timedelta(hours=12 * (snapshots - 1 - n))
        teams = []
        for rank in range(1, players + 1):
            picks: list[dict] = []
            while len(picks) < config.TEAM_SIZE:
                drift = [w * (1 + 0.15 * rng.random()) * (1.6 if rank <= 50 and i < 6 else 1) for i, w in enumerate(weights)]
                choice = rng.choices(pool, weights=drift)[0]
                if choice not in picks:
                    picks.append(choice)
            teams.append(
                SourceTeam(
                    rank_min=rank, rank_max=rank, trophies=7000 - rank * 3,
                    members=[SourceCreature(c["key"], c["name"], c["rarity"]) for c in picks],
                    player_key=f"example-player-{rank}", fingerprint=f"example-{n}-{rank}",
                )
            )
        snap = SourceSnapshot(
            source=SOURCE_NAME, source_snapshot_id=f"example-{n}", captured_at=captured, kind="arena",
            leaderboard_id="example", leaderboard_name="EXAMPLE DATA - randomly generated, not real",
            teams=teams, declared_team_count=players,
        )
        ingest.store(conn, ingest.prepare(snap), now=now, is_example=True)
    conn.close()
    return path
