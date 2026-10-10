"""Tournaments that fix every creature's level or switch stat boosts off (rules as the real feed publishes them)."""
import dataclasses
import json
import tempfile
from pathlib import Path

from jwa_tracker import builds, db, ingest, site
from jwa_tracker.sources import jwa_dashboard_feed
from tests.helpers import T0, FakeSource, TempDataDir, full_team, hours, snapshot
from tests.test_site import fake_version, no_roster

# REAL rule blocks from the jwa-dashboard feed (Cash Tournament 2026-10-W1 and the arena season).
CASH_EVENT = {"name": "[Tournament] 2026-10-W1 - Cash Tournament", "is_arena": False, "standardized_level": 35,
              "stat_boosts_enabled": False}
CASH_DISPLAY = {"show_enhancements": False, "show_levels": False, "show_stat_boosts": False, "standardized_level": 35}
ARENA_DISPLAY = {"show_enhancements": True, "show_levels": True, "show_stat_boosts": True, "standardized_level": None}
CASH_RULES = {"levels": False, "boosts": False, "enhancements": False, "standardized_level": 35}


class ParseRulesTests(TempDataDir):
    def test_feed_rules(self):
        self.assertEqual(jwa_dashboard_feed.parse_build_rules({"display_rules": CASH_DISPLAY}, CASH_EVENT), CASH_RULES)
        self.assertEqual(jwa_dashboard_feed.parse_build_rules({"display_rules": ARENA_DISPLAY}, {"is_arena": True}),
                         {"levels": True, "boosts": True, "enhancements": True, "standardized_level": None})
        self.assertIsNone(jwa_dashboard_feed.parse_build_rules({}, {}))

    def test_compact_form(self):
        self.assertEqual(site.compact_rules(CASH_RULES), {"lv": 0, "bo": 0, "en": 0, "std": 35})
        self.assertEqual(site.compact_rules({"levels": True, "boosts": True, "enhancements": True}), {})
        self.assertEqual(site.compact_rules(None), {})


class StoredRulesTests(TempDataDir):
    def test_rules_published_per_snapshot(self):
        cash = dataclasses.replace(snapshot([full_team(r) for r in range(1, 51)], snap_id="cash", kind="tournament"),
                                   build_rules=CASH_RULES)
        arena = snapshot([full_team(r) for r in range(1, 51)], snap_id="arena", captured_at=T0 + hours(1))
        ingest.store(self.conn, ingest.prepare(cash), now=T0)
        ingest.store(self.conn, ingest.prepare(arena), now=T0)
        records = {r["id"].split(":")[1]: r for r in site.build_local(self.conn, T0)["snapshots"]}
        self.assertEqual(records["cash"]["br"], {"lv": 0, "bo": 0, "en": 0, "std": 35})
        self.assertEqual(records["arena"]["br"], {})

    def test_older_snapshots_get_their_rules_once(self):
        cash = dataclasses.replace(snapshot([full_team(1)], snap_id="cash", kind="tournament"), build_rules=CASH_RULES)
        sid = ingest.store(self.conn, ingest.prepare(dataclasses.replace(cash, build_rules=None)), now=T0)
        self.conn.execute("DELETE FROM snapshot_rules WHERE snapshot_id = ?", (sid,))  # as an older version left it
        src = FakeSource([cash])
        builds.backfill(self.conn, [src], now=T0)
        self.assertEqual(db.build_rules(self.conn)[sid], CASH_RULES)
        builds.backfill(self.conn, [src], now=T0)
        self.assertEqual(src.fetched, ["cash"])  # read once; builds were already there and stay untouched


class ArchiveRulesTests(TempDataDir):
    def build(self, source, archive, out):
        return site.build_website(
            out, archive, now_fn=lambda: T0 + hours(48), sources=[source],
            collect_kwargs={"roster_fetch": no_roster, "version_fetch": fake_version, "fetch_images": False},
        )

    def test_archived_tournaments_get_rules_from_the_feed_once(self):
        tmp = Path(tempfile.mkdtemp())
        archive, out = tmp / "archive", tmp / "site"
        cash = dataclasses.replace(snapshot([full_team(r) for r in range(1, 51)], snap_id="2026-10-09_20-50-04",
                                            kind="tournament"), build_rules=CASH_RULES)
        self.build(FakeSource([cash]), archive, out)
        for path in (archive / "snapshots").glob("*.json"):  # an archive written before rules were kept
            recs = json.loads(path.read_text(encoding="utf-8"))
            for r in recs:
                r.pop("br", None)
            path.write_text(json.dumps(recs), encoding="utf-8")
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)
        src = FakeSource([cash])
        self.build(src, archive, out)
        self.assertEqual(src.fetched, ["2026-10-09_20-50-04"])
        data = json.loads((out / "data" / "site.json").read_text(encoding="utf-8"))
        self.assertEqual(data["snapshots"][0]["br"], {"lv": 0, "bo": 0, "en": 0, "std": 35})
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)
        src = FakeSource([cash])
        self.build(src, archive, out)
        self.assertEqual(src.fetched, [])  # now complete: never downloaded again
