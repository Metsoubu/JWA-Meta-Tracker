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
from tests.helpers import T0, TempDataDir

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
