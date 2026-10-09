"""Site data export, the public-website build/archive, and body-type icons (synthetic data)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jwa_tracker import analysis, config, images, ingest, net, site, standins, stats
from tests.helpers import KEYS, T0, FakeSource, TempDataDir, full_team, hours, snapshot


def pooled(records, kind, range_key, ids=None, version=None):
    """Mirror of the browser's pooling (web/app.js `pool`), used to check the export."""
    total, counts = 0, {}
    for r in records:
        if r["k"] != kind or (ids is not None and r["id"] not in ids) or (version and r["v"] != version):
            continue
        u = r["u"].get(range_key)
        if not u:
            continue
        total += u["n"]
        for k, c in u["c"].items():
            counts[k] = counts.get(k, 0) + c
    return total, counts


class ExportMatchesReferenceTests(TempDataDir):
    """The numbers in site.json must equal the tested Python reference (analysis.py)."""

    def setUp(self):
        super().setUp()
        early = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)   # v3.22
        late = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)    # v3.23
        specs = [
            ("s1", early, lambda r: ("junior",) if r % 3 == 0 else ()),
            ("s2", late, lambda r: ("junior", "paralidactylus") if r <= 30 else ("tyrannotor",)),
            ("s3", late + hours(12), lambda r: ("paralidactylus",) if r > 50 else ("junior",)),
        ]
        for sid, when, first in specs:
            teams = [full_team(lo, first_keys=first(lo), rank_max=lo + 9) for lo in range(1, 100, 10)
                     for _ in range(1)]
            # ten teams per band, as in the real feed
            teams = [full_team(lo, first_keys=first(lo + i), rank_max=lo + 9) for lo in range(1, 100, 10) for i in range(10)]
            for i, t in enumerate(teams):
                t.fingerprint = f"{sid}-{i}"
            ingest.store(self.conn, ingest.prepare(snapshot(teams, snap_id=sid, captured_at=when)), now=T0)
        self.payload = site.build_local(self.conn, T0 + hours(48))
        self.analyzer = analysis.Analyzer(self.conn)

    def test_every_range_and_selection_matches(self):
        recs = self.payload["snapshots"]
        snaps = {s["source_snapshot_id"]: s for s in self.analyzer.snapshots("arena")}
        selections = [(f"snap:{snaps[sid]['id']}", {f"test-source:{sid}"}, None) for sid in ("s1", "s2", "s3")]
        selections += [("ver:v3.22", None, "v3.22"), ("ver:v3.23", None, "v3.23")]
        for range_key in ("top50", "top100", "r51-100"):
            for spec, ids, version in selections:
                with self.subTest(range=range_key, sel=spec):
                    ref = self.analyzer.tierlist("arena", range_key, spec)
                    total, counts = pooled(recs, "arena", range_key, ids=ids, version=version)
                    self.assertEqual(total, ref["sample"]["teams"])
                    ref_used = {c["key"]: c for c in ref["creatures"] if c["count"]}
                    self.assertEqual(counts, {k: c["count"] for k, c in ref_used.items()})
                    for k, c in ref_used.items():
                        self.assertEqual(stats.tier_for(counts[k], total), c["tier"])
                        self.assertAlmostEqual(stats.percentage(counts[k], total), c["pct"])

    def test_versions_tagged(self):
        self.assertEqual([r["v"] for r in self.payload["snapshots"]], ["v3.22", "v3.23", "v3.23"])

    def test_only_used_creatures_listed(self):
        used = site.used_keys(self.payload["snapshots"])
        self.assertEqual(set(self.payload["creatures"]), used)
        self.assertIn("junior", used)
        self.assertNotIn("dodo", self.payload["creatures"])

    def test_ranges_beyond_coverage_are_not_offered(self):
        self.assertEqual(set(self.payload["snapshots"][0]["u"]), {"top50", "top100", "r51-100"})
        offered = [r["key"] for r in self.payload["ranges"]]
        self.assertNotIn("r1-50", offered)  # same players as Top 50


class SanitizeTests(unittest.TestCase):
    def test_paths_removed(self):
        msg = r"[Errno 2] No such file: 'C:\Users\someone\AppData\Local\x.db' and /home/someone/x.txt"
        out = site.sanitize(msg)
        self.assertNotIn("someone", out)
        self.assertIn("<file>", out)


def fake_version():
    return "3.23.44", datetime(2026, 9, 29, 15, 8, 22, tzinfo=timezone.utc)


def no_roster(*a, **k):
    raise net.FetchError("offline in tests", transient=True)


class WebsiteBuildTests(TempDataDir):
    def build(self, source, archive, out):
        return site.build_website(
            out, archive, now_fn=lambda: T0 + hours(48), sources=[source], repo_url="https://github.com/example/jwa",
            collect_kwargs={"roster_fetch": no_roster, "version_fetch": fake_version, "fetch_images": False},
        )

    def test_history_survives_and_is_not_downloaded_twice(self):
        tmp = Path(tempfile.mkdtemp())
        archive, out = tmp / "archive", tmp / "site"
        first = [snapshot([full_team(r) for r in range(1, 51)], snap_id="2026-10-01_10-00-00", captured_at=T0),
                 snapshot([full_team(r) for r in range(1, 51)], snap_id="2026-10-01_22-00-00", captured_at=T0 + hours(12))]
        r1 = self.build(FakeSource(first), archive, out)
        self.assertEqual((r1["added"], r1["total"]), (2, 2))

        # Next run: the feed has dropped its old files and published one new snapshot.
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)  # a fresh, empty build database, like a new GitHub Actions run
        newer = snapshot([full_team(r, first_keys=("junior",)) for r in range(1, 51)], snap_id="2026-10-02_10-00-00",
                         captured_at=T0 + hours(24))
        src = FakeSource([first[1], newer])
        r2 = self.build(src, archive, out)
        self.assertEqual((r2["added"], r2["total"]), (1, 3))
        self.assertEqual(src.fetched, ["2026-10-02_10-00-00"])  # archived snapshot not downloaded again

        data = json.loads((out / "data" / "site.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "website")
        self.assertEqual(data["repo_url"], "https://github.com/example/jwa")
        self.assertEqual(len(data["snapshots"]), 3)
        self.assertEqual(len(data["runs"]), 2)
        self.assertIn("junior", data["creatures"])
        for name in ("index.html", "app.js", "styles.css", "theme.js", "favicon.svg", ".nojekyll"):
            self.assertTrue((out / name).exists(), name)
        self.assertTrue(any((out / "img" / "silhouettes").glob("*.svg")))
        self.assertFalse((out / "img" / "silhouettes" / "credits.json").exists())
        self.assertEqual(len(list((archive / "snapshots").glob("*.json"))), 2)  # one file per day

    def test_failed_update_still_publishes_existing_history(self):
        tmp = Path(tempfile.mkdtemp())
        archive, out = tmp / "archive", tmp / "site"
        self.build(FakeSource([snapshot([full_team(1)], snap_id="2026-10-01_10-00-00")]), archive, out)
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)
        r = self.build(FakeSource(list_error=net.FetchError("down", transient=True)), archive, out)
        self.assertEqual((r["status"], r["total"]), ("failed", 1))
        data = json.loads((out / "data" / "site.json").read_text(encoding="utf-8"))
        self.assertEqual(data["runs"][0]["status"], "failed")
        self.assertEqual(len(data["snapshots"]), 1)


class BodyTypeIconTests(unittest.TestCase):
    def test_every_hand_checked_body_type_has_a_picture(self):
        credits = images.load_credits()
        missing = {k: v for k, v in standins.BODY_OVERRIDES.items() if credits.get(v, {}).get("status") != "ok"}
        self.assertEqual(missing, {})

    def test_fusion_tree_prefers_the_ingredient_sharing_the_name(self):
        credits = {"baryonyx": {"status": "ok"}, "megaraptor": {"status": "ok"}}
        tree = {
            "baryotor": ["megamimus", "baryosaurus"],
            "megamimus": ["megaraptor", "procerathomimus"],
            "baryosaurus": ["baryothus", "sarcosaurus"],
            "baryothus": ["leucistic_baryonyx", "dreadnoughtus"],
        }
        with mock.patch.object(standins, "BODY_OVERRIDES", {}):
            via, path = standins.resolve("baryotor", tree, credits)
        self.assertEqual(via, "baryonyx")
        self.assertEqual(path, ["baryotor", "baryosaurus", "baryothus", "leucistic_baryonyx"])

    def test_falls_back_to_any_ancestor_with_a_picture(self):
        with mock.patch.object(standins, "BODY_OVERRIDES", {}):
            via, _ = standins.resolve("x", {"x": ["a", "b"], "a": ["c"]}, {"b": {"status": "ok"}})
            self.assertEqual(via, "b")
            via, _ = standins.resolve("x", {"x": ["a"]}, {})
            self.assertIsNone(via)

    def test_picture_priority(self):
        pics = standins.picture_index(["tyrannosaurus_rex", "baryotor", "unmatched-x"])
        self.assertEqual(pics["tyrannosaurus_rex"]["kind"], "silhouette")
        self.assertEqual(pics["baryotor"]["kind"], "body-type")
        self.assertTrue(pics["baryotor"]["credit"].startswith("Body-type icon:"))
        self.assertNotIn("unmatched-x", pics)
        with mock.patch.object(images, "custom_images", return_value={"baryotor": "img/custom/baryotor.png"}):
            self.assertEqual(standins.picture_index(["baryotor"])["baryotor"]["kind"], "custom")

    def test_every_creature_in_real_data_has_a_picture(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "real_feed_snapshot_2026-10-08_21-31-45.json")
                             .read_text(encoding="utf-8"))
        from jwa_tracker import roster

        resolver = roster.Resolver(roster.load_seed_roster())
        keys = set()
        for team in fixture["anonymized_teams"]:
            for c in team["creatures"]:
                m = resolver.resolve("jwa-dashboard-feed", c["creature_id"], c["display_name"], c["rarity"])
                if m.key:
                    keys.add(m.key)
        pics = standins.picture_index(keys)
        self.assertEqual(sorted(keys - set(pics)), [])


if __name__ == "__main__":
    unittest.main()
