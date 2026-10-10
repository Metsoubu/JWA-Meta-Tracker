"""Creature builds, team details and the creature panel's numbers (synthetic data unless noted)."""
import dataclasses
import gzip
import http.client
import json
import sqlite3
import tempfile
from pathlib import Path

from jwa_tracker import builds, config, db, ingest, net, server, site, stats
from jwa_tracker.sources import jwa_dashboard_feed
from jwa_tracker.sources.base import Build, SourceCreature
from tests.helpers import KEYS, T0, FakeSource, TempDataDir, full_team, hours, snapshot
from tests.test_site import fake_version, no_roster

FEED = jwa_dashboard_feed.SOURCE_NAME


def feed_creature(cid, name, rarity="Apex", level=35.0, enh=5, boosts=None, omega=None):
    return {"creature_id": cid, "display_name": name, "rarity": rarity, "level": level, "enhancement_level": enh,
            "stat_boosts": boosts if boosts is not None else {"Attack": 20.0, "Health": 18.0, "Speed": 0.0},
            "omega_training_points": omega or {}, "omega_ability_unlocks": []}


def feed_doc(teams):
    return json.dumps({
        "schema_version": "jwa-dashboard.public.v2", "captured_at": "2026-10-01T12:00:00+00:00",
        "captured_team_count": len(teams), "leaderboard_id": "726",
        "event": {"name": "[Seasonal] Test", "is_arena": True}, "anonymized_teams": teams,
    }).encode()


def with_builds(team, rng_seed):
    """Give every member of a synthetic team a deterministic build."""
    members = [dataclasses.replace(m, build=Build(35.0, (rng_seed + i) % 6, (("Attack", 20.0), ("Health", float(i)),
                                                                          ("Speed", 0.0)), None))
               for i, m in enumerate(team.members)]
    team.members = members
    return team


class FeedBuildParsingTests(TempDataDir):
    def test_builds_are_read_from_the_feed(self):
        names = ["BARYOTOR", "SINRAPTOR"] + [f"X{i}" for i in range(6)]
        creatures = [feed_creature(f"id{i}", n) for i, n in enumerate(names)]
        creatures[1] = feed_creature("id1", "SINRAPTOR", rarity="Omega", enh=0, boosts={"Attack": 20.0, "Health": 0.0},
                                     omega={"Health": 89.0, "Attack": 65.0, "Crit Dmg": 50.0})
        snap = jwa_dashboard_feed.parse_snapshot(feed_doc([{"leaderboard_rank_band": "1-10", "creatures": creatures}]),
                                                 "2026-10-01_12-00-00")
        bary, sin = snap.teams[0].members[:2]
        self.assertEqual(bary.build, Build(35.0, 5, (("Attack", 20.0), ("Health", 18.0), ("Speed", 0.0)), None))
        self.assertEqual(sin.build.omega, (("Attack", 65.0), ("Crit Dmg", 50.0), ("Health", 89.0)))
        self.assertEqual(dict(sin.build.boosts), {"Attack": 20.0, "Health": 0.0})  # Speed not published

    def test_junk_values_are_ignored(self):
        c = feed_creature("a", "A", level="35", enh=True, boosts={"Attack": -1, "Health": float("nan"), "": 3})
        self.assertEqual(jwa_dashboard_feed.parse_build(c), Build(None, None, (), None))
        self.assertIsNone(jwa_dashboard_feed.parse_build({"creature_id": "a"}))


class StoreAndExportTests(TempDataDir):
    def setUp(self):
        super().setUp()
        teams = []
        for lo in range(1, 100, 10):
            for i in range(10):
                t = full_team(lo, first_keys=("junior",) if (lo + i) % 3 == 0 else (), rank_max=lo + 9)
                t.fingerprint = f"s1-{lo}-{i}"
                teams.append(with_builds(t, lo + i))
        self.snap_id = ingest.store(self.conn, ingest.prepare(snapshot(teams, snap_id="s1")), now=T0)

    def test_builds_are_stored_and_append_only(self):
        n = self.conn.execute("SELECT COUNT(*) FROM member_builds").fetchone()[0]
        self.assertEqual(n, 800)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE member_builds SET level = 1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM member_builds")
        status = self.conn.execute("SELECT status FROM snapshot_builds WHERE snapshot_id = ?", (self.snap_id,)).fetchone()
        self.assertEqual(status[0], "recorded")

    def test_details_round_trip_and_match_the_usage_counts(self):
        details = site.team_details(self.conn)
        teams = details["test-source:s1"]
        self.assertEqual(len(teams), 100)
        self.assertEqual(teams[0][2][0][1:4], (35, 1 % 6, {"Attack": 20, "Health": 0, "Speed": 0}))
        doc = json.loads(json.dumps(site.encode_details(details)))
        self.assertEqual(site.decode_details(doc), details)
        self.assertEqual(site.encode_details(site.decode_details(doc)), doc)
        record = site.snapshot_records(self.conn)[0]
        for range_key, block in record["u"].items():
            lo, hi, _ = stats.rank_filter(range_key)
            for key, count in block["c"].items():
                p = stats.creature_profile(teams, key, lo, hi)
                self.assertEqual((p["used"], p["teams"]), (count, block["n"]), (range_key, key))

    def test_local_site_points_to_monthly_detail_files(self):
        payload = site.build_local(self.conn, T0)
        self.assertEqual(payload["snapshots"][0]["d"], 1)
        month = site.details_month_local(self.conn, "2026-10")
        self.assertEqual(set(month["snaps"]), {"test-source:s1"})
        self.assertEqual(site.details_month_local(self.conn, "2026-09")["snaps"], {})
        with self.assertRaises(ValueError):
            site.details_month_local(self.conn, "../x")


class ProfileTests(TempDataDir):
    def test_teammates_rank_groups_and_builds(self):
        def m(key, boosts=None, level=35, enh=0, omega=None):
            return (key, level, enh, boosts, omega)
        a = {"Attack": 20, "Health": 18, "Speed": 0}
        b = {"Attack": 20, "Health": 0, "Speed": 15}
        teams = [
            (1, 10, [m("x", a, enh=5), m("y"), m("z")]),
            (1, 10, [m("x", a, enh=5), m("y")]),
            (11, 20, [m("x", b, omega={"Health": 10, "Attack": 20}), m("z")]),
            (11, 20, [m("y"), m("z")]),
            (51, 60, [m("x", None, None, None)]),          # an older team without a build
            (61, 70, [("x", None, None, None, None)]),
        ]
        p = stats.creature_profile(teams, "x", 1, 100)
        self.assertEqual((p["teams"], p["used"]), (6, 5))
        self.assertEqual([(g["lo"], g["teams"], g["used"]) for g in p["groups"]][:3], [(1, 2, 2), (11, 2, 1), (21, 0, 0)])
        self.assertEqual(p["teammates"], [("y", 2), ("z", 2)])
        B = p["builds"]
        self.assertEqual(B["count"], 3)
        self.assertEqual(B["splits"], [((20, 18, 0), 2), ((20, 0, 15), 1)])
        self.assertEqual(B["complete"], 3)
        self.assertEqual(B["top_builds"], [((35, 5, 20, 18, 0), 2), ((35, 0, 20, 0, 15), 1)])
        self.assertEqual(B["boost_avg"], {"Attack": 20, "Health": 12, "Speed": 5})
        self.assertEqual(B["levels"], [(35, 3)])
        self.assertEqual(B["enhancements"], [(5, 2), (0, 1)])
        self.assertEqual((B["omegas"], B["omega_avg"]), (1, {"Health": 10, "Attack": 20}))
        top50 = stats.creature_profile(teams, "x", 1, 50)
        self.assertEqual((top50["teams"], top50["used"], len(top50["groups"])), (4, 3, 5))

    def test_rank_groups(self):
        self.assertEqual(stats.rank_groups(1, 100)[-1], (91, 100))
        self.assertEqual(len(stats.rank_groups(51, 100)), 5)
        self.assertEqual(stats.rank_groups(1, 500)[:2], [(1, 50), (51, 100)])
        self.assertEqual(stats.rank_groups(101, 250)[-1], (226, 250))


class BackfillTests(TempDataDir):
    def _store_as_older_version(self, snap):
        """Store a snapshot the way versions before builds did: no builds, no build status."""
        stripped = dataclasses.replace(snap, teams=[dataclasses.replace(
            t, members=[dataclasses.replace(m, build=None) for m in t.members]) for t in snap.teams])
        sid = ingest.store(self.conn, ingest.prepare(stripped), now=T0)
        self.conn.execute("DELETE FROM snapshot_builds WHERE snapshot_id = ?", (sid,))
        return sid

    def test_fills_builds_from_the_source_once(self):
        teams = [with_builds(full_team(r), r) for r in range(1, 21)]
        snap = snapshot(teams, snap_id="old")
        sid = self._store_as_older_version(snap)
        src = FakeSource([snap])
        self.assertEqual(builds.backfill(self.conn, [src], now=T0), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM member_builds").fetchone()[0], 160)
        self.assertEqual(self.conn.execute("SELECT status FROM snapshot_builds WHERE snapshot_id=?", (sid,)).fetchone()[0], "filled")
        self.assertEqual(builds.backfill(self.conn, [src], now=T0), 0)
        self.assertEqual(src.fetched, ["old"])  # never downloaded again

    def test_filled_in_even_when_no_update_is_due(self):
        from jwa_tracker import collector

        snap = snapshot([with_builds(full_team(r), r) for r in range(1, 11)], snap_id="old")
        self._store_as_older_version(snap)
        now = T0 + hours(1)
        run = db.start_run(self.conn, "scheduled", now)
        db.finish_run(self.conn, run, "success", now=now)  # just checked: nothing is due
        result = collector.run_collection("scheduled", force=False, sources=[FakeSource([snap])], conn=self.conn,
                                          now_fn=lambda: now, roster_fetch=no_roster, version_fetch=fake_version,
                                          fetch_images=False)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM member_builds").fetchone()[0], 80)

    def test_new_imports_are_never_downloaded_again(self):
        ingest.store(self.conn, ingest.prepare(snapshot([full_team(1)], snap_id="new")), now=T0)
        src = FakeSource([snapshot([full_team(1)], snap_id="new")])
        builds.backfill(self.conn, [src], now=T0)
        self.assertEqual(src.fetched, [])

    def test_unavailable_snapshots_are_not_retried(self):
        snap = snapshot([full_team(1)], snap_id="gone")
        sid = self._store_as_older_version(snap)
        src = FakeSource([snap], fetch_errors={"gone": net.FetchError("404", transient=False)})
        self.assertEqual(builds.backfill(self.conn, [src], now=T0), 0)
        builds.backfill(self.conn, [src], now=T0)
        self.assertEqual(src.fetched, ["gone"])
        self.assertEqual(self.conn.execute("SELECT status FROM snapshot_builds WHERE snapshot_id=?", (sid,)).fetchone()[0], "unavailable")

    def test_temporary_errors_are_retried(self):
        snap = snapshot([full_team(1)], snap_id="later")
        self._store_as_older_version(snap)
        src = FakeSource([snap], fetch_errors={"later": net.FetchError("timeout", transient=True)})
        builds.backfill(self.conn, [src], now=T0)
        builds.backfill(self.conn, [src], now=T0)
        self.assertEqual(src.fetched, ["later", "later"])

    def test_saved_raw_copy_is_used_without_downloading(self):
        creatures = [feed_creature(f"id{i}", n) for i, n in enumerate(
            ["BARYOTOR", "JUNIOR", "BECKLERIZAURUS", "SHANTROSPINOS", "ATROCOMAXIMA", "TITANOTHOLUS", "BECKLEJARA",
             "SPINOTOPS"])]
        raw = feed_doc([{"leaderboard_rank_band": "1-10", "creatures": creatures}])
        folder = config.raw_dir() / FEED
        folder.mkdir(parents=True)
        (folder / "2026-10-01_12-00-00.json.gz").write_bytes(gzip.compress(raw))
        self._store_as_older_version(jwa_dashboard_feed.parse_snapshot(raw, "2026-10-01_12-00-00"))

        def no_network(*a, **k):
            raise AssertionError("should not download")

        feed = jwa_dashboard_feed.JwaDashboardFeed(fetch=no_network, fetch_json=no_network)
        self.assertEqual(builds.backfill(self.conn, [feed], now=T0), 1)
        details = site.team_details(self.conn)["jwa-dashboard-feed:2026-10-01_12-00-00"]
        self.assertEqual(details[0][2][0][1:4], (35, 5, {"Attack": 20, "Health": 18, "Speed": 0}))


class WebsiteDetailsTests(TempDataDir):
    def build(self, source, archive, out):
        return site.build_website(
            out, archive, now_fn=lambda: T0 + hours(48), sources=[source],
            collect_kwargs={"roster_fetch": no_roster, "version_fetch": fake_version, "fetch_images": False},
        )

    def fresh_db(self):
        self.conn.close()
        TempDataDir.tearDown(self)
        TempDataDir.setUp(self)

    def test_details_are_archived_published_and_backfilled_once(self):
        tmp = Path(tempfile.mkdtemp())
        archive, out = tmp / "archive", tmp / "site"
        snaps = [snapshot([with_builds(full_team(r), r) for r in range(1, 51)], snap_id=sid, captured_at=when)
                 for sid, when in (("2026-09-30_10-00-00", T0 - hours(48)), ("2026-10-01_10-00-00", T0))]
        self.build(FakeSource(snaps), archive, out)
        self.assertEqual(len(list((archive / "teams").glob("*.json"))), 2)
        self.assertEqual(sorted(p.name for p in (out / "data" / "details").glob("*.json")), ["2026-09.json", "2026-10.json"])
        data = json.loads((out / "data" / "site.json").read_text(encoding="utf-8"))
        self.assertTrue(all(s["d"] == 1 for s in data["snapshots"]))
        october = json.loads((out / "data" / "details" / "2026-10.json").read_text(encoding="utf-8"))
        self.assertEqual(list(october["snaps"]), ["test-source:2026-10-01_10-00-00"])

        # An archive written before details existed: the snapshots are fetched once more to add them.
        records_before = sorted(p.read_text(encoding="utf-8") for p in (archive / "snapshots").glob("*.json"))
        for p in (archive / "teams").glob("*.json"):
            p.unlink()
        self.fresh_db()
        src = FakeSource(snaps)
        self.build(src, archive, out)
        self.assertEqual(sorted(src.fetched), ["2026-09-30_10-00-00", "2026-10-01_10-00-00"])
        self.assertEqual(sorted(p.read_text(encoding="utf-8") for p in (archive / "snapshots").glob("*.json")),
                         records_before)  # history itself untouched
        self.assertEqual(len(list((archive / "teams").glob("*.json"))), 2)

        # From then on nothing is downloaded twice.
        self.fresh_db()
        src = FakeSource(snaps)
        self.build(src, archive, out)
        self.assertEqual(src.fetched, [])


class ServerDetailsTests(TempDataDir):
    def test_served_and_paths_checked(self):
        ingest.store(self.conn, ingest.prepare(snapshot([with_builds(full_team(1), 1)], snap_id="a")), now=T0)
        srv = server.serve_in_thread(config.db_path())
        port = srv.server_address[1]
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                body = r.read()
                c.close()
                return r.status, body
            status, body = get("/data/details/2026-10.json")
            self.assertEqual(status, 200)
            doc = json.loads(body)
            self.assertEqual(list(doc["snaps"]), ["test-source:a"])
            self.assertEqual(doc["boost_stats"], ["Attack", "Health", "Speed"])
            for bad in ("/data/details/2026-1.json", "/data/details/..%2fsite.json", "/data/details/x.json"):
                self.assertEqual(get(bad)[0], 404, bad)
        finally:
            srv.shutdown()
            srv.server_close()
