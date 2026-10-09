"""Validation, duplicates, persistence and immutability of stored snapshots."""
import sqlite3
import unittest
from unittest import mock

from jwa_tracker import analysis, db, ingest, roster
from tests.helpers import KEYS, T0, TempDataDir, full_team, hours, snapshot, team


class ValidationTests(unittest.TestCase):
    def test_valid_team(self):
        self.assertIsNone(ingest.validate_team(full_team(1)))

    def test_missing_creature(self):
        self.assertIn("7 creatures", ingest.validate_team(team(1, KEYS[:7])))

    def test_too_many_creatures(self):
        self.assertIn("9 creatures", ingest.validate_team(team(1, KEYS[:9])))

    def test_duplicate_creature_in_team(self):
        self.assertEqual(ingest.validate_team(team(1, KEYS[:7] + [KEYS[0]])), "same creature listed twice")

    def test_missing_or_bad_rank(self):
        self.assertEqual(ingest.validate_team(team(None, KEYS[:8], rank_max=None)), "missing rank")
        self.assertEqual(ingest.validate_team(team(5, KEYS[:8], rank_max=3)), "invalid rank")
        self.assertEqual(ingest.validate_team(team(0, KEYS[:8])), "invalid rank")

    def test_creature_without_id(self):
        bad = team(1, KEYS[:7] + [""])
        self.assertEqual(ingest.validate_team(bad), "creature without an id")


class DedupeTests(unittest.TestCase):
    def test_same_player_counted_once_best_rank_kept(self):
        snap = snapshot([full_team(9, player="Alice"), full_team(3, player="alice"), full_team(4, player="Bob")])
        prepared = ingest.prepare(snap)
        self.assertEqual(prepared.duplicates_dropped, 1)
        self.assertEqual(sorted(t.team.rank_min for t in prepared.teams), [3, 4])

    def test_identical_records_removed_when_fingerprint_identifies_player(self):
        a = full_team(1, rank_max=10)
        dup = full_team(1, rank_max=10)
        other = full_team(1, first_keys=("junior",), rank_max=10)
        other.fingerprint = "different"
        prepared = ingest.prepare(snapshot([a, dup, other], identifies=True))
        self.assertEqual(prepared.duplicates_dropped, 1)
        self.assertEqual(len(prepared.teams), 2)

    def test_identical_teams_kept_when_band_has_room_and_records_are_coarse(self):
        # Two different players may genuinely use the same eight creatures.
        prepared = ingest.prepare(snapshot([full_team(1, rank_max=10), full_team(1, rank_max=10)], identifies=False))
        self.assertEqual(prepared.duplicates_dropped, 0)
        self.assertEqual(len(prepared.teams), 2)

    def test_over_capacity_band_drops_exact_duplicates(self):
        # Rank 5 can only hold one player: an identical second record is a duplicate.
        prepared = ingest.prepare(snapshot([full_team(5), full_team(5)], identifies=False))
        self.assertEqual(prepared.duplicates_dropped, 1)

    def test_invalid_teams_kept_but_flagged(self):
        prepared = ingest.prepare(snapshot([full_team(1), team(2, KEYS[:5])]))
        self.assertEqual(prepared.valid_count, 1)
        self.assertEqual(prepared.invalid_count, 1)
        self.assertEqual(prepared.rank_coverage_max, 1)


class StorageTests(TempDataDir):
    def store(self, snap):
        return ingest.store(self.conn, ingest.prepare(snap), now=T0)

    def test_persists_across_connections(self):
        sid = self.store(snapshot([full_team(r) for r in range(1, 11)]))
        self.conn.close()
        self.conn = db.connect()
        snap = db.get_snapshot(self.conn, sid)
        self.assertEqual(snap["teams_valid"], 10)
        self.assertEqual(len(db.load_team_rows(self.conn, [sid])), 80)

    def test_duplicate_snapshot_rejected(self):
        self.store(snapshot([full_team(1)]))
        with self.assertRaises(ingest.DuplicateSnapshot):
            self.store(snapshot([full_team(1)]))
        self.assertEqual(len(db.list_snapshots(self.conn)), 1)

    def test_history_is_read_only(self):
        sid = self.store(snapshot([full_team(1)]))
        for sql in (
            "UPDATE snapshots SET teams_valid = 99",
            "DELETE FROM snapshots",
            "UPDATE teams SET rank_min = 2",
            "DELETE FROM teams",
            "UPDATE team_members SET source_creature_id = 'x'",
            "DELETE FROM team_members",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)
        self.assertEqual(db.get_snapshot(self.conn, sid)["teams_valid"], 1)

    def test_failed_import_leaves_nothing_behind(self):
        self.store(snapshot([full_team(1)], snap_id="good"))
        with mock.patch.object(roster, "register_source_creatures", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.store(snapshot([full_team(1)], snap_id="bad", captured_at=T0 + hours(12)))
        self.assertEqual([s["source_snapshot_id"] for s in db.list_snapshots(self.conn)], ["good"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0], 1)

    def test_snapshot_without_valid_teams_is_refused(self):
        with self.assertRaises(ValueError):
            self.store(snapshot([team(1, KEYS[:3])]))
        self.assertEqual(db.list_snapshots(self.conn), [])

    def test_invalid_teams_not_counted_in_usage(self):
        self.store(snapshot([full_team(1), full_team(2), team(3, ["junior"] * 1 + KEYS[:6])]))
        a = analysis.Analyzer(self.conn)
        tl = a.tierlist("arena", "top500", "latest")
        self.assertEqual(tl["sample"]["teams"], 2)
        self.assertEqual(tl["sample"]["excluded_invalid"], 1)

    def test_tierlist_end_to_end_with_rank_filters(self):
        teams = [full_team(r, first_keys=("junior",) if r <= 25 else ()) for r in range(1, 101)]
        self.store(snapshot(teams))
        a = analysis.Analyzer(self.conn)
        top50 = {c["key"]: c for c in a.tierlist("arena", "top50", "latest")["creatures"]}
        self.assertEqual(top50["junior"]["count"], 25)
        self.assertEqual(top50["junior"]["pct"], 50.0)
        self.assertEqual(top50["junior"]["tier"], "B")
        r51 = {c["key"]: c for c in a.tierlist("arena", "r51-100", "latest")["creatures"]}
        self.assertEqual(r51["junior"]["pct"], 0.0)
        top500 = a.tierlist("arena", "top500", "latest")
        self.assertEqual(top500["sample"]["state"], "partial")
        self.assertEqual(top500["sample"]["teams"], 100)
        none = a.tierlist("arena", "r101-250", "latest")
        self.assertEqual(none["sample"]["state"], "none")
        self.assertEqual(none["sample"]["teams"], 0)
        # every roster creature is present, even unused ones
        self.assertGreater(len(top50), 500)

    def test_history_kept_and_compared(self):
        self.store(snapshot([full_team(r) for r in range(1, 11)], snap_id="old", captured_at=T0))
        self.store(snapshot([full_team(r, first_keys=("junior",)) for r in range(1, 11)], snap_id="new",
                            captured_at=T0 + hours(12)))
        a = analysis.Analyzer(self.conn)
        snaps = db.list_snapshots(self.conn, "arena")
        self.assertEqual(len(snaps), 2)
        cmp = a.compare("arena", "top100", f"snap:{snaps[1]['id']}", "latest")
        rows = {r["key"]: r for r in cmp["rows"]}
        self.assertEqual(rows["junior"]["base_pct"], 0.0)
        self.assertEqual(rows["junior"]["pct"], 100.0)
        self.assertEqual(rows["junior"]["delta_pp"], 100.0)
        self.assertIn("junior", [r["key"] for r in cmp["risers"]])
        old_view = a.tierlist("arena", "top100", f"snap:{snaps[1]['id']}")
        self.assertEqual({c["key"]: c for c in old_view["creatures"]}["junior"]["count"], 0)

    def test_version_pooling(self):
        # v3.22 before 2026-09-29T15:08Z, v3.23 after (bundled timeline)
        from datetime import datetime, timezone
        early = datetime(2026, 9, 26, tzinfo=timezone.utc)
        late = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.store(snapshot([full_team(1, first_keys=("junior",))], snap_id="a", captured_at=early))
        self.store(snapshot([full_team(1)], snap_id="b", captured_at=late))
        self.store(snapshot([full_team(1, first_keys=("junior",))], snap_id="c", captured_at=late + hours(12)))
        a = analysis.Analyzer(self.conn)
        listing = a.snapshot_list("arena")
        self.assertEqual({v["version"]: v["snapshot_count"] for v in listing["versions"]}, {"v3.22": 1, "v3.23": 2})
        pooled = a.tierlist("arena", "top50", "ver:v3.23")
        junior = {c["key"]: c for c in pooled["creatures"]}["junior"]
        self.assertEqual((junior["count"], junior["total"]), (1, 2))
        self.assertEqual(pooled["previous"]["version"], "v3.22")


class NameResolutionTests(TempDataDir):
    def resolve(self, name, rarity=None, source_id="x"):
        return roster.resolver_for(self.conn).resolve("jwa-dashboard-feed", source_id, name, rarity)

    def test_real_feed_names(self):
        cases = {
            ("BARYOTOR", "Apex"): "baryotor",
            ("Tyrannoto", "Apex"): "tyrannotor",            # truncated internal id
            ("Paralidac", "Apex"): "paralidactylus",
            ("TYRANNOSAURUS", "Epic"): "tyrannosaurus_rex",  # rarity picks the epic T. rex
            ("KLUGES MÄDCHEN", "Omega"): "clever_girl",      # German name, alias
            ("WOLLNASHORN", "Epic"): "woolly_rhino",
            ("BRÜNETTE", "Epic"): "brunette",
            ("BLUE\r\n", "Epic"): "blue",
            ("OVIRAPTOR GEN\xa02", "Epic"): "oviraptor_gen_2",
            ("Cryolopho2", "Unique"): "cryolophosaurus_mattel",
            ("DRACO INTREPIDUS ", "Apex"): "draco_intrepidus",
        }
        for (name, rarity), key in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.resolve(name, rarity).key, key)

    def test_unknown_placeholder_is_not_guessed(self):
        m = self.resolve("05865dc2", "Unknown")
        self.assertIsNone(m.key)
        self.assertEqual(m.method, "unmatched")

    def test_unmatched_creature_gets_clear_label(self):
        with db.transaction(self.conn):
            roster.register_source_creatures(self.conn, "feed", [("05865dc28682e494", "05865dc2", "Unknown")], T0)
        row = self.conn.execute("SELECT * FROM source_creatures").fetchone()
        creature = db.all_creatures(self.conn)[row["creature_key"]]
        self.assertEqual(creature["name"], "Unidentified creature (05865dc2)")
        self.assertEqual(creature["in_roster"], 0)


if __name__ == "__main__":
    unittest.main()
