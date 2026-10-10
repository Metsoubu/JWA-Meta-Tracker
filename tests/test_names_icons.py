"""Creature names the feed publishes in German or without a name, re-labelling, and icons.

The names below are REAL labels seen in the jwa-dashboard feed (2026-09/10); the teams are synthetic.
"""
import json
import tempfile
from pathlib import Path

from jwa_tracker import db, ingest, roster, site, standins
from jwa_tracker.sources.base import SourceCreature
from tests.helpers import T0, FakeSource, TempDataDir, full_team, hours, snapshot
from tests.test_site import fake_version, no_roster

FEED_NAMES = {
    "WIEDERGEBURT-T-REX": "rebirth_t_rex",
    "WIEDERGEBURT-RAPTOR": "rebirth_raptor",
    "WIEDERGEBURT-SPINOSAURUS": "rebirth_spinosaurus",
    "WIEDERGEBURT-SPINOSAURUS GEN\xa02": "rebirth_spinosaurus_gen_2",  # with a non-breaking space, as published
    "TYRANNOSAURUS-WEIBCHEN": "tyrannosaur_doe",
    "Quetzalco3": "rebirth_quetzalcoatlus",
    "KLUGES MÄDCHEN": "clever_girl",
    "REXY": "rexy",
}


class GermanNameTests(TempDataDir):
    def test_real_feed_names_are_identified(self):
        resolver = roster.Resolver(roster.load_seed_roster())
        for name, key in FEED_NAMES.items():
            with self.subTest(name=name):
                self.assertEqual(resolver.resolve("jwa-dashboard-feed", "x", name, "Legendary").key, key)
        self.assertEqual(resolver.resolve("f", "x", "WIEDERGEBURT-T-REX", "Legendary").method, "translated")

    def test_nameless_creatures_are_not_guessed(self):
        resolver = roster.Resolver(roster.load_seed_roster())
        for code in ("2b88a0f8", "433c1948", "05865dc2"):
            self.assertIsNone(resolver.resolve("jwa-dashboard-feed", code + "7fd53094", code, "Unknown").key)

    def test_old_unmatched_names_are_fixed_on_the_next_check(self):
        from jwa_tracker import collector

        t = full_team(1)
        t.members[0] = SourceCreature("id-rebirth", "WIEDERGEBURT-T-REX", "Legendary")
        no_translation = roster.Resolver(roster.load_seed_roster(), aliases={})
        with db.transaction(self.conn):  # how an older version stored it: not identified
            roster.register_source_creatures(self.conn, "test-source", [("id-rebirth", "WIEDERGEBURT-T-REX", "Legendary")],
                                             T0, no_translation)
        ingest.store(self.conn, ingest.prepare(snapshot([t] + [full_team(r) for r in range(2, 51)])), now=T0)
        before = site.build_local(self.conn, T0)["snapshots"][0]["u"]["top50"]["c"]
        self.assertIn("unmatched-testsource-idrebirth", before)
        collector.run_collection("scheduled", force=False, sources=[FakeSource([])], conn=self.conn,
                                 now_fn=lambda: T0 + hours(1), roster_fetch=no_roster, version_fetch=fake_version,
                                 fetch_images=False)
        payload = site.build_local(self.conn, T0)
        after = payload["snapshots"][0]["u"]["top50"]["c"]
        self.assertEqual(after.get("rebirth_t_rex"), 1)
        self.assertNotIn("unmatched-testsource-idrebirth", after)
        self.assertIn("Wiedergeburt-t-rex", payload["creatures"]["rebirth_t_rex"]["aka"])  # shown under "How the data source names it"


class ArchiveRelabelTests(TempDataDir):
    def test_archived_history_gets_proper_names_and_keeps_counts(self):
        folder = Path(tempfile.mkdtemp())
        archive = site.Archive(folder)
        old = "unmatched-jwadashboard-c7f5952be9fd1d04"
        nameless = "unmatched-jwadashboard-2b88a0f87fd53094"
        archive.records["feed:a"] = {"id": "feed:a", "t": "2026-10-09T20:50:04Z", "k": "tournament", "src": "feed",
                                     "u": {"top100": {"n": 100, "c": {old: 60, nameless: 70, "pierce": 61}}}}
        archive.creatures = {old: {"name": "Wiedergeburt-t-rex", "rarity": "legendary", "listed": False,
                                   "aka": ["Wiedergeburt-T-Rex"]},
                             nameless: {"name": "Unidentified creature (2b88a0f8)", "rarity": "unknown",
                                        "listed": False, "aka": ["2b88a0f8"]}}
        archive.details["feed:a"] = [(1, 10, [(old, 35, 0, {"Attack": 20}, None), ("pierce", 35, 0, None, None)])]
        mapping = archive.relabel(roster.Resolver(roster.load_seed_roster()))
        self.assertEqual(mapping, {old: "rebirth_t_rex"})
        self.assertEqual(archive.records["feed:a"]["u"]["top100"]["c"], {nameless: 70, "pierce": 61, "rebirth_t_rex": 60})
        self.assertEqual(archive.details["feed:a"][0][2][0][0], "rebirth_t_rex")
        self.assertEqual(archive.creatures["rebirth_t_rex"]["aka"], ["Wiedergeburt-T-Rex"])
        self.assertIn(nameless, archive.creatures)  # no name: left as it is, never guessed
        self.assertEqual(archive.relabel(roster.Resolver(roster.load_seed_roster())), {})


class IconTests(TempDataDir):
    def test_named_and_rebirth_creatures_have_icons(self):
        keys = ["rexy", "pierce", "angel", "rebel", "monkeydactyl", "panthera_blytheae", "rativates",
                *FEED_NAMES.values(), "tyrannosaur_buck"]
        pictures = standins.picture_index(keys)
        self.assertEqual([k for k in keys if k not in pictures], [])
        self.assertIn("Kentrosaurus", pictures["pierce"]["credit"])
