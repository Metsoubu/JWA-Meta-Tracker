"""What happens when a brand-new creature shows up on top teams (synthetic data)."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from jwa_tracker import analysis, db, ingest, roster, site, standins
from jwa_tracker.sources.base import SourceCreature
from tests.helpers import KEYS, T0, TempDataDir, full_team, hours, snapshot

NEW = {"key": "newdinoraptor", "name": "Newdinoraptor", "rarity": "apex", "class": "fierce",
       "hybrid_type": "mega_hybrid", "added_version": "v3.24"}


def team_with(source_id, display_name, rarity, rank):
    t = full_team(rank)
    t.members[0] = SourceCreature(source_id, display_name, rarity)
    return t


def dinodex_html(items):
    data = {"props": {"pageProps": {"dex": {"items": items}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'.encode()


class NewCreatureTests(TempDataDir):
    def test_counted_immediately_and_named_once_the_creature_list_knows_it(self):
        # 1. The feed reports a creature the creature list does not know yet.
        teams = [team_with("id-new", "NEWDINORAPTOR", "Apex", r) if r <= 10 else full_team(r) for r in range(1, 101)]
        ingest.store(self.conn, ingest.prepare(snapshot(teams)), now=T0)
        rows = {c["name"]: c for c in analysis.Analyzer(self.conn).tierlist("arena", "top100", "latest")["creatures"]}
        self.assertEqual(rows["Newdinoraptor"]["count"], 10)        # counted right away
        self.assertFalse(rows["Newdinoraptor"]["in_roster"])         # shown as "unverified name"

        # 2. The daily creature-list refresh adds it: the same teams are re-labelled automatically.
        items = roster.load_seed_roster() + [NEW]
        msg = roster.refresh_roster_if_due(self.conn, T0 + hours(30), fetch=lambda url, **k: dinodex_html(items), force=True)
        self.assertIn("1 previously unmatched", msg)
        tl = analysis.Analyzer(self.conn).tierlist("arena", "top100", "latest")
        new = {c["key"]: c for c in tl["creatures"]}["newdinoraptor"]
        self.assertEqual((new["count"], new["in_roster"], new["rarity"]), (10, True, "apex"))
        payload = site.build_local(self.conn, T0 + hours(30))
        self.assertEqual(payload["creatures"]["newdinoraptor"]["name"], "Newdinoraptor")

    def test_placeholder_name_fixed_when_the_feed_learns_the_real_name(self):
        ingest.store(self.conn, ingest.prepare(snapshot([team_with("abc", "05865dc2", "Unknown", 1)], snap_id="a")), now=T0)
        key = self.conn.execute("SELECT creature_key FROM source_creatures WHERE source_creature_id='abc'").fetchone()[0]
        self.assertTrue(key.startswith("unmatched-"))
        # A later snapshot carries the proper name for the same creature id.
        ingest.store(self.conn, ingest.prepare(snapshot([team_with("abc", "BARYOTOR", "Apex", 1)], snap_id="b",
                                                        captured_at=T0 + hours(12))), now=T0)
        key = self.conn.execute("SELECT creature_key FROM source_creatures WHERE source_creature_id='abc'").fetchone()[0]
        self.assertEqual(key, "baryotor")
        # ...and the earlier snapshot is corrected too, because teams point at the creature id.
        old = analysis.Analyzer(self.conn).snapshots("arena")[-1]
        rows = {c["key"]: c for c in analysis.Analyzer(self.conn).tierlist("arena", "top100", f"snap:{old['id']}")["creatures"]}
        self.assertEqual(rows["baryotor"]["count"], 1)

    def test_new_hybrid_gets_a_body_type_icon_automatically(self):
        roster.store_roster(self.conn, roster.load_seed_roster() + [NEW], "test", T0)
        page = json.dumps({"props": {"pageProps": {"detail": {
            "ingredients": ["baryotor", "something"],
            "evolutionData": {"newdinoraptor": {"ingredients": ["baryotor", "something"]},
                              "baryotor": {"ingredients": ["megamimus", "baryosaurus"]}}}}}})
        html = f'<script id="__NEXT_DATA__" type="application/json">{page}</script>'.encode()
        tmp = Path(tempfile.mkdtemp()) / "standins.json"
        with mock.patch.object(standins, "STANDIN_FILE", tmp):
            added = standins.update(self.conn, ["newdinoraptor"], fetch=lambda url, **k: html, pause=0)
            self.assertEqual(added, 1)
            pic = standins.picture_index(["newdinoraptor"])["newdinoraptor"]
        self.assertEqual(pic["kind"], "body-type")
        self.assertIn("Baryonyx", pic["credit"])  # baryotor's hand-checked body type
