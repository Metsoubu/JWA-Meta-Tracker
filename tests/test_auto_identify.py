"""Automatic handling of new creatures: German names, nameless codes, skeleton-family icons.

Creature names and codes are REAL (jwa-dashboard feed, Paleo.gg); the teams are synthetic.
"""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jwa_tracker import db, identify, images, ingest, roster, site, standins
from jwa_tracker.sources import jwa_dashboard_feed
from tests.helpers import T0, FakeSource, TempDataDir
from tests.test_site import fake_version

CODE = "2b88a0f87fd5309418417842511ab488"
NAMED = ["PIERCE", "BECKLOLYTH", "NYCTOSAURUS", "SHANTUNRAX", "MASIAKASAURUS", "SINRAPTOR", "PARALITITAN", "REXY"]


def without_hand_checked_codes():
    real = roster.load_aliases()
    return mock.patch.object(roster, "load_aliases",
                             lambda: {k: v for k, v in real.items() if k not in ("by_source_id", "identified")})


def tournament(snap_id, captured, eligible=None, code_level=16.0):
    creatures = [{"creature_id": f"id-{n.lower()}", "display_name": n, "rarity": "Legendary", "level": 16.0,
                  "enhancement_level": 0, "stat_boosts": {}, "omega_training_points": {}} for n in NAMED]
    code = {"creature_id": CODE, "display_name": CODE[:8], "rarity": "Unknown", "level": code_level,
            "enhancement_level": 0, "stat_boosts": {}, "omega_training_points": {}}
    rules = [{"kind": "rarity", "label": "Allowed rarities", "values": ["Legendary", "Omega"]}]
    if eligible:
        rules.append({"kind": "creatures", "label": "Eligible creatures", "values": eligible})
    doc = {"schema_version": "jwa-dashboard.public.v2", "captured_at": captured, "captured_team_count": 10,
           "leaderboard_id": "1", "display_rules": {"show_levels": False, "show_stat_boosts": False,
                                                     "show_enhancements": False, "standardized_level": 35},
           "event": {"name": "[Tournament] 2026-10-W1 - Cash Tournament", "is_arena": False,
                     "standardized_level": 35, "stat_boosts_enabled": False, "tournament_rules": rules},
           # ten teams, each: the nameless creature plus 7 of the 8 named ones
           "anonymized_teams": [{"leaderboard_rank_band": "1-10",
                                 "creatures": [code] + [c for j, c in enumerate(creatures) if j != i % len(NAMED)]}
                                for i in range(10)]}
    return jwa_dashboard_feed.parse_snapshot(json.dumps(doc).encode(), snap_id)


def bands_tournament(snap_id, eligible=None, code_name=None, code_rarity="Unknown"):
    """Top 50 teams in rank bands; every team uses the nameless creature (or, later, its real name)."""
    code = {"creature_id": CODE, "display_name": code_name or CODE[:8], "rarity": code_rarity, "level": 16.0,
            "enhancement_level": 0, "stat_boosts": {}, "omega_training_points": {}}
    others = [{"creature_id": f"id-{n.lower()}", "display_name": n, "rarity": "Legendary", "level": 16.0,
               "enhancement_level": 0, "stat_boosts": {}, "omega_training_points": {}} for n in NAMED[:7]]
    rules = [{"kind": "rarity", "label": "Allowed rarities", "values": ["Legendary", "Omega", "Apex"]}]
    if eligible:
        rules.append({"kind": "creatures", "label": "Eligible creatures", "values": eligible})
    teams = []
    for lo in range(1, 50, 10):
        for i in range(10):
            team = [dict(code)] + [dict(c, level=16.0 + lo + i) for c in others]  # different teams, one per player
            teams.append({"leaderboard_rank_band": f"{lo}-{lo + 9}", "creatures": team})
    doc = {"schema_version": "jwa-dashboard.public.v2", "captured_at": "2026-10-09T20:50:00+00:00",
           "captured_team_count": len(teams), "leaderboard_id": "1",
           "display_rules": {"show_levels": False, "show_stat_boosts": False, "show_enhancements": False,
                             "standardized_level": 35},
           "event": {"name": "[Tournament] 2026-10-W1 - Cash Tournament", "is_arena": False, "standardized_level": 35,
                     "stat_boosts_enabled": False, "tournament_rules": rules},
           "anonymized_teams": teams}
    return jwa_dashboard_feed.parse_snapshot(json.dumps(doc).encode(), snap_id)


class FeedSource(FakeSource):
    name = jwa_dashboard_feed.SOURCE_NAME


def seed_dinodex(url, **_kwargs):
    """The creature list page (English or German), offline."""
    items = [{**c, "version": c.get("added_version")} for c in roster.load_seed_roster()]
    data = {"props": {"pageProps": {"dex": {"items": items}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'.encode()


class GermanNameTests(TempDataDir):
    def test_official_german_names_from_the_creature_list(self):
        roster.store_localized_names(self.conn, "de", {"marsupial_lion": "BEUTELLÖWE", "tsintamoth": "TSINTAMUT"})
        resolver = roster.resolver_for(self.conn)
        self.assertEqual(resolver.resolve("feed", "x", "BEUTELLÖWE", "Rare"), roster.Match("marsupial_lion", "localized"))
        self.assertEqual(resolver.resolve("feed", "x", "TSINTAMUT", "Legendary").key, "tsintamoth")

    def test_english_names_win_over_german_ones(self):
        roster.store_localized_names(self.conn, "de", {"tsintamoth": "Baryotor"})  # a clash on purpose
        self.assertEqual(roster.resolver_for(self.conn).resolve("feed", "x", "BARYOTOR", "Apex").key, "baryotor")

    def test_bundled_list_carries_german_names(self):
        self.assertEqual(self.conn.execute("SELECT name FROM creature_names WHERE key='marsupial_lion' AND lang='de'")
                         .fetchone()[0], "BEUTELLÖWE")


class NamelessCodeTests(TempDataDir):
    def store(self, snap):
        ingest.store(self.conn, ingest.prepare(snap), now=T0)

    def list_refreshed(self, when):
        with db.transaction(self.conn):
            db.set_meta(self.conn, "roster_fetched_at", db.iso(when))

    def code_key(self):
        return self.conn.execute("SELECT creature_key, match_method FROM source_creatures WHERE source_creature_id = ?",
                                 (CODE,)).fetchone()

    def test_alphabetical_place_identifies_it_after_the_waiting_days(self):
        with without_hand_checked_codes():
            self.store(tournament("2026-10-09_20-50-04", "2026-10-09T20:50:00+00:00",
                                  eligible=["NYCTOSAURUS", "PARALITITAN", CODE[:8], "PIERCE", "SHANTUNRAX"]))
            self.list_refreshed(T0 + timedelta(days=8, hours=21))  # one day after it first appeared: wait
            identify.run(self.conn, T0)
            self.assertEqual(tuple(self.code_key())[1], "unmatched")
            self.assertIn("named only once the creature list has had 3 days",
                          identify.notes(self.conn)[roster.unresolved_key("jwa-dashboard-feed", CODE)])
            self.list_refreshed(T0 + timedelta(days=12))
            self.assertEqual(identify.run(self.conn, T0), {CODE: "pelorosuchus"})
            self.assertEqual(tuple(self.code_key()), ("pelorosuchus", "deduced"))
            note = identify.notes(self.conn)["pelorosuchus"]
            for evidence in ("Worked out automatically", "allowed only Legendary and Omega", "lowest level is 16",
                             "between Paralititan and Pierce"):
                self.assertIn(evidence, note)
            self.assertEqual(site.creature_info(self.conn, ["pelorosuchus"])["pelorosuchus"]["note"], note)  # on its page

    def test_several_fits_stay_unidentified_with_the_possibilities(self):
        with without_hand_checked_codes():
            self.store(tournament("2026-10-09_20-50-04", "2026-10-09T20:50:00+00:00"))
            self.list_refreshed(T0 + timedelta(days=30))
            self.assertEqual(identify.run(self.conn, T0), {})
            d = self.conn.execute("SELECT * FROM deductions").fetchone()
            self.assertIsNone(d["creature_key"])
            self.assertIn("pelorosuchus", json.loads(d["possible"]))
            self.assertIn("It is probably", d["note"])

    def test_a_conclusion_is_withdrawn_when_new_creatures_make_it_unclear(self):
        with without_hand_checked_codes():
            self.store(tournament("2026-10-09_20-50-04", "2026-10-09T20:50:00+00:00",
                                  eligible=["PARALITITAN", CODE[:8], "PIERCE"]))
            self.list_refreshed(T0 + timedelta(days=30))
            identify.run(self.conn, T0)
            self.assertEqual(tuple(self.code_key())[0], "pelorosuchus")
            roster.store_roster(self.conn, [{"key": "pelodactylus", "name": "Pelodactylus", "rarity": "legendary",
                                             "class": "cunning", "hybrid_type": "hybrid", "added_version": "v3.24"}],
                                "test", T0)  # a new Legendary that also fits between Paralititan and Pierce
            identify.run(self.conn, T0)
            self.assertEqual(tuple(self.code_key()), (roster.unresolved_key("jwa-dashboard-feed", CODE), "unmatched"))

    def test_hand_checked_identifications_are_left_alone(self):
        self.store(tournament("2026-10-09_20-50-04", "2026-10-09T20:50:00+00:00"))
        identify.run(self.conn, T0)
        self.assertEqual(tuple(self.code_key()), ("pelorosuchus", "alias"))

    def test_website_keeps_conclusions_between_runs(self):
        with without_hand_checked_codes():
            self.store(tournament("2026-10-09_20-50-04", "2026-10-09T20:50:00+00:00",
                                  eligible=["PARALITITAN", CODE[:8], "PIERCE"]))
            self.list_refreshed(T0 + timedelta(days=30))
            identify.run(self.conn, T0)
            saved = identify.export(self.conn)
            self.conn.close()
            TempDataDir.tearDown(self)
            TempDataDir.setUp(self)
            identify.load(self.conn, saved)
            resolver = roster.resolver_for(self.conn)
            self.assertEqual(resolver.resolve("jwa-dashboard-feed", CODE, CODE[:8], "Unknown"),
                             roster.Match("pelorosuchus", "deduced"))
            self.assertEqual(resolver.unmatched_alias[roster.unresolved_key("jwa-dashboard-feed", CODE)], "pelorosuchus")


def rare_epic_tournament(snap_id, codes, eligible=None):
    """Like the real Cash Tournament of 6 November 2026: rarities only, several nameless codes at Epic levels."""
    named = ["GIGANTSPINOSAURUS", "COELOTATOR", "PANTHERA", "KENTROSAURUS", "DEINOTOPS", "BARYOTHUS", "Guidraco"]
    others = [{"creature_id": f"id-{n.lower()}", "display_name": n, "rarity": "Epic", "level": 20.0,
               "enhancement_level": 0, "stat_boosts": {}, "omega_training_points": {}} for n in named]
    rules = [{"kind": "rarity", "label": "Allowed rarities", "values": ["Rare", "Epic"]}]
    if eligible:
        rules.append({"kind": "creatures", "label": "Eligible creatures", "values": eligible})
    teams = [{"leaderboard_rank_band": "1-10",
              "creatures": [{"creature_id": codes[i % len(codes)], "display_name": codes[i % len(codes)][:8],
                             "rarity": "Unknown", "level": 11.0, "enhancement_level": 0, "stat_boosts": {},
                             "omega_training_points": {}}] + others} for i in range(10)]
    doc = {"schema_version": "jwa-dashboard.public.v2", "captured_at": "2026-11-06T22:03:32+00:00",
           "captured_team_count": len(teams), "leaderboard_id": "738",
           "display_rules": {"show_levels": False, "show_stat_boosts": False, "show_enhancements": False,
                             "standardized_level": 35},
           "event": {"name": "[Tournament] 2026-11-W1 - Cash Tournament", "is_arena": False, "standardized_level": 35,
                     "stat_boosts_enabled": False, "tournament_rules": rules},
           "anonymized_teams": teams}
    return jwa_dashboard_feed.parse_snapshot(json.dumps(doc).encode(), snap_id)


class SeveralCodesTests(TempDataDir):
    """Two nameless codes cannot be the same creature (real creatures and codes, synthetic teams)."""
    SNAP = "2026-11-06_22-03-26"
    CODES = ["788dcb3260710214ca81ea153c41f9c3", "45f4a51647b11ca4db3b34b728f1d045", "44ec1b9095916674795d34bcafc444e3"]
    # named by the feed by now, so they are not candidates (Arsinoitherium through a hand-checked code)
    HISTORY = {"alioramus": {"aka": ["Alioramu"], "rarity": "epic"}, "arsinoitherium": {"aka": ["05865dc2"], "rarity": "rare"}}

    def store(self, *args, **kwargs):
        ingest.store(self.conn, ingest.prepare(rare_epic_tournament(self.SNAP, *args, **kwargs)), now=T0)
        with db.transaction(self.conn):
            db.set_meta(self.conn, "roster_fetched_at", db.iso(T0 + timedelta(days=60)))

    def deductions(self):
        return {d["source_id"]: d for d in self.conn.execute("SELECT * FROM deductions")}

    def test_newer_creatures_named_out_of_order_do_not_rule_out_older_ones(self):
        self.store(self.CODES[:1])
        identify.run(self.conn, T0, archive_creatures=self.HISTORY)
        possible = set(json.loads(self.deductions()[self.CODES[0]]["possible"]))
        self.assertEqual(possible, {"arsionosaurus", "koolatrodon", "peloroplites"})  # not only the 3.23 one
        self.assertIn("official name list goes up to game version 3.20", self.deductions()[self.CODES[0]]["note"])

    def test_three_codes_and_three_creatures_stay_possibilities(self):
        self.store(self.CODES)
        self.assertEqual(identify.run(self.conn, T0, archive_creatures=self.HISTORY), {})
        for d in self.deductions().values():
            self.assertIsNone(d["creature_key"])
            self.assertIn("It is probably", d["note"])
            self.assertEqual(set(json.loads(d["possible"])), {"arsionosaurus", "koolatrodon", "peloroplites"})

    def test_more_codes_than_creatures_names_none_of_them(self):
        ruled_out = {**self.HISTORY, "arsionosaurus": {"aka": ["Arsinosau"], "rarity": "epic"},
                     "koolatrodon": {"aka": ["Koolatrod"], "rarity": "epic"}}
        self.store(self.CODES)
        for _ in range(2):  # the same answer every time, rather than flipping back and forth
            self.assertEqual(identify.run(self.conn, T0, archive_creatures=ruled_out), {})
            for d in self.deductions().values():
                self.assertIsNone(d["creature_key"])
                self.assertEqual(json.loads(d["possible"]), ["peloroplites"])
                self.assertIn("3 different unnamed creatures fit only Peloroplites", d["note"])
                self.assertIn("at least 2 of them must be a creature the creature list does not have yet", d["note"])
        rows = self.conn.execute("SELECT DISTINCT match_method FROM source_creatures WHERE source_creature_id IN (?, ?, ?)",
                                 self.CODES).fetchall()
        self.assertEqual([r[0] for r in rows], ["unmatched"])

    def test_a_code_that_is_sure_settles_the_others(self):
        first, second = self.CODES[:2]
        history = {**self.HISTORY, "arsionosaurus": {"aka": ["Arsinosau"], "rarity": "epic"}}
        # Alphabetical place pins the first code to Koolatrodon; only Peloroplites is then left for the second.
        self.store([first, second], eligible=["KENTROSAURUS", first[:8], "PANTHERA", second[:8]])
        for _ in range(2):  # and the same answer on the next check
            self.assertEqual(identify.run(self.conn, T0, archive_creatures=history), {first: "koolatrodon", second: "peloroplites"})


class SkeletonIconTests(TempDataDir):
    def test_creature_without_picture_borrows_its_skeleton_mates_icon(self):
        tmp = Path(tempfile.mkdtemp())
        families = {"charlie": {"family": "family_002"}, "velociraptor": {"family": "family_002"},
                    "utahraptor": {"family": "family_002"}, "tyrannosaurus_rex": {"family": "family_001"}}
        (tmp / "families.json").write_text(json.dumps(families), encoding="utf-8")
        (tmp / "standins.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(standins, "FAMILY_FILE", tmp / "families.json"), \
                mock.patch.object(standins, "STANDIN_FILE", tmp / "standins.json"), \
                mock.patch.object(standins, "BODY_OVERRIDES", {}):
            pics = standins.picture_index(["charlie", "tyrannosaurus_rex"])
        self.assertTrue(images.load_credits()["velociraptor"]["status"] == "ok")
        self.assertIn(pics["charlie"]["url"], ("img/silhouettes/velociraptor.svg", "img/silhouettes/utahraptor.svg"))
        self.assertIn("same in-game skeleton", pics["charlie"]["credit"])
        self.assertEqual(pics["tyrannosaurus_rex"]["kind"], "silhouette")

    def test_skeleton_family_read_from_the_creature_page(self):
        page = '<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"detail": ' \
               '{"animation_set": "family_002", "ingredients": []}}}}</script>'
        self.assertEqual(standins.parse_family(page), "family_002")


class WebsiteHistoryTests(TempDataDir):
    """The website starts every build with an empty database: what it learned must carry over."""
    FIRST = "2026-10-09_20-50-04"
    SEEN = T0 + timedelta(days=8, hours=9)  # just after that snapshot was captured

    def build(self, source, tmp, when):
        return site.build_website(
            tmp / "site", tmp / "archive", now_fn=lambda: when, sources=[source],
            collect_kwargs={"roster_fetch": seed_dinodex, "version_fetch": fake_version, "fetch_images": False},
        )

    def fresh_db(self):
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)

    def site_data(self, tmp):
        return json.loads((tmp / "site" / "data" / "site.json").read_text(encoding="utf-8"))

    def test_the_wait_ends_even_though_the_code_is_not_in_any_new_snapshot(self):
        tmp = Path(tempfile.mkdtemp())
        with without_hand_checked_codes():
            snap = bands_tournament(self.FIRST, eligible=["PARALITITAN", CODE[:8], "PIERCE"])
            self.build(FeedSource([snap]), tmp, self.SEEN)
            placeholder = roster.unresolved_key("jwa-dashboard-feed", CODE)
            self.assertIn(placeholder, self.site_data(tmp)["creatures"])  # waiting: not named yet
            self.assertIn("named only once", self.site_data(tmp)["creatures"][placeholder]["note"])

            self.fresh_db()
            source = FeedSource([])  # the feed has dropped that old file: only the kept evidence is left
            self.build(source, tmp, self.SEEN + timedelta(days=4))
            self.assertEqual(source.fetched, [])
            data = self.site_data(tmp)
            self.assertNotIn(placeholder, data["creatures"])
            self.assertEqual(data["snapshots"][0]["u"]["top50"]["c"]["pelorosuchus"], 50)  # counts unchanged, proper name
            self.assertIn("Worked out automatically", data["creatures"]["pelorosuchus"]["note"])
            self.assertEqual(data["unmatched"], [])

            self.fresh_db()
            self.build(FeedSource([]), tmp, self.SEEN + timedelta(days=5))  # and it stays that way
            self.assertIn("pelorosuchus", self.site_data(tmp)["creatures"])

    def test_history_is_corrected_when_the_feed_names_the_code_later(self):
        tmp = Path(tempfile.mkdtemp())
        with without_hand_checked_codes():
            self.build(FeedSource([bands_tournament(self.FIRST)]), tmp, self.SEEN)
            placeholder = roster.unresolved_key("jwa-dashboard-feed", CODE)
            self.assertIn(placeholder, self.site_data(tmp)["creatures"])

            self.fresh_db()
            # The feed republishes the same snapshot, now with the creature's real name.
            source = FeedSource([bands_tournament(self.FIRST, code_name="Quetzalvexus", code_rarity="Apex")])
            self.build(source, tmp, self.SEEN + timedelta(hours=6))
            self.assertEqual(source.fetched, [self.FIRST])  # read again because it showed an unidentified creature
            data = self.site_data(tmp)
            self.assertNotIn(placeholder, data["creatures"])
            self.assertEqual(data["snapshots"][0]["u"]["top50"]["c"]["quetzalvexus"], 50)
            self.assertEqual(data["unmatched"], [])
            self.assertNotIn("note", data["creatures"]["quetzalvexus"])  # named by the feed: nothing to explain

            self.fresh_db()
            source = FeedSource([bands_tournament(self.FIRST, code_name="Quetzalvexus", code_rarity="Apex")])
            self.build(source, tmp, self.SEEN + timedelta(hours=12))
            self.assertEqual(source.fetched, [])  # settled: never downloaded again

    def test_creatures_named_earlier_in_history_are_ruled_out(self):
        with without_hand_checked_codes():
            ingest.store(self.conn, ingest.prepare(tournament(self.FIRST, "2026-10-09T20:50:00+00:00")), now=T0)
            with db.transaction(self.conn):
                db.set_meta(self.conn, "roster_fetched_at", db.iso(T0 + timedelta(days=30)))
            identify.run(self.conn, T0)
            before = self.conn.execute("SELECT creature_key, possible FROM deductions").fetchone()
            self.assertIsNone(before["creature_key"])
            self.assertEqual(set(json.loads(before["possible"])), {"pelorosuchus", "erlidraco", "rebirth_quetzalcoatlus"})
            history = {"erlidraco": {"aka": ["Erlidraco"], "rarity": "legendary"},
                       "rebirth_quetzalcoatlus": {"aka": ["Quetzalco3"], "rarity": "legendary"}}
            self.assertEqual(identify.run(self.conn, T0, archive_creatures=history), {CODE: "pelorosuchus"})

    def test_a_real_name_replaces_the_conclusion_on_the_pc_too(self):
        with without_hand_checked_codes():
            ingest.store(self.conn, ingest.prepare(tournament(self.FIRST, "2026-10-09T20:50:00+00:00",
                                                              eligible=["PARALITITAN", CODE[:8], "PIERCE"])), now=T0)
            with db.transaction(self.conn):
                db.set_meta(self.conn, "roster_fetched_at", db.iso(T0 + timedelta(days=30)))
            identify.run(self.conn, T0)
            with db.transaction(self.conn):
                roster.register_source_creatures(self.conn, "jwa-dashboard-feed", [(CODE, "PELOROSUCHUS", "Legendary")], T0)
            self.assertEqual(identify.run(self.conn, T0), {})
            row = self.conn.execute("SELECT creature_key, match_method FROM source_creatures WHERE source_creature_id = ?",
                                    (CODE,)).fetchone()
            self.assertEqual(tuple(row), ("pelorosuchus", "exact"))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0], 0)
