"""Test helpers. Everything here is SYNTHETIC test data unless a name says 'real'."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jwa_tracker import db, roster, versions
from jwa_tracker.sources.base import SnapshotRef, SourceCreature, SourceSnapshot, SourceTeam

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# Real roster keys, used to build synthetic teams. "junior" is deliberately NOT
# in this filler list: tests add it to chosen teams as a marker creature.
KEYS = [
    "becklerizaurus", "shantrospinos", "baryotor", "atrocomaxima", "titanotholus", "becklejara",
    "spinotops", "proto_lux", "centrolophus_lux", "distortus_rex", "aquignathus", "rajadorixis",
]


class TempDataDir(unittest.TestCase):
    """Gives each test its own empty data folder and database."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="jwa-test-")
        self._old = os.environ.get("JWA_TRACKER_DATA_DIR")
        os.environ["JWA_TRACKER_DATA_DIR"] = self._tmp
        self.conn = db.connect()
        roster.ensure_roster(self.conn, T0)
        with db.transaction(self.conn):
            versions.seed_known_versions(self.conn, T0)

    def tearDown(self):
        self.conn.close()
        if self._old is None:
            os.environ.pop("JWA_TRACKER_DATA_DIR", None)
        else:
            os.environ["JWA_TRACKER_DATA_DIR"] = self._old
        shutil.rmtree(self._tmp, ignore_errors=True)


def team(rank_min, keys, rank_max=None, player=None, fingerprint=None, trophies=None):
    rank_max = rank_min if rank_max is None else rank_max
    members = [SourceCreature(k, k.replace("_", " ").title(), None) for k in keys]
    return SourceTeam(
        rank_min=rank_min, rank_max=rank_max, members=members, trophies=trophies, player_key=player,
        fingerprint=fingerprint or f"{rank_min}-{rank_max}-{'|'.join(keys)}-{player}",
    )


def snapshot(teams, snap_id="s1", captured_at=T0, kind="arena", source="test-source", identifies=False):
    return SourceSnapshot(
        source=source, source_snapshot_id=snap_id, captured_at=captured_at, kind=kind,
        leaderboard_id="L1", leaderboard_name="Synthetic test leaderboard", teams=teams,
        declared_team_count=len(teams), fingerprint_identifies_player=identifies,
    )


def full_team(rank, first_keys=(), rank_max=None, player=None):
    """An 8-creature team that starts with `first_keys` and is filled from KEYS."""
    keys = list(first_keys)
    for k in KEYS:
        if len(keys) == 8:
            break
        if k not in keys:
            keys.append(k)
    return team(rank, keys, rank_max=rank_max, player=player)


class FakeSource:
    """A data source that serves prepared snapshots (or failures) without any network."""

    name = "test-source"
    label = "Synthetic test source"

    def __init__(self, snapshots=None, list_error=None, fetch_errors=None):
        self.snapshots = {s.source_snapshot_id: s for s in (snapshots or [])}
        self.list_error = list_error
        self.fetch_errors = fetch_errors or {}
        self.fetched = []

    def list_snapshots(self):
        if self.list_error:
            raise self.list_error
        return sorted(
            (SnapshotRef(id=s.source_snapshot_id, captured_at=s.captured_at) for s in self.snapshots.values()),
            key=lambda r: r.captured_at,
        )

    def fetch_snapshot(self, ref):
        self.fetched.append(ref.id)
        if ref.id in self.fetch_errors:
            raise self.fetch_errors[ref.id]
        return self.snapshots[ref.id], b"{}"


def hours(n):
    return timedelta(hours=n)
