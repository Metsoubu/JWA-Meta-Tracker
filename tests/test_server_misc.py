"""Dashboard server, security checks, CSV import, versions and demo isolation."""
import http.client
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jwa_tracker import config, db, ingest, server, versions
from jwa_tracker.sources import csv_import
from tests.helpers import T0, TempDataDir, full_team, hours, snapshot


class ServerTests(TempDataDir):
    def setUp(self):
        super().setUp()
        teams = [full_team(r, first_keys=("junior",) if r <= 40 else ()) for r in range(1, 101)]
        ingest.store(self.conn, ingest.prepare(snapshot(teams, snap_id="a", captured_at=T0)), now=T0)
        ingest.store(self.conn, ingest.prepare(snapshot(teams[:50], snap_id="b", captured_at=T0 + hours(12))), now=T0)
        self.srv = server.serve_in_thread(config.db_path(), demo=True)
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def request(self, method, path, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, headers=h)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheaders(), body

    def get_json(self, path):
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_binds_to_localhost_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertEqual(config.HOST, "127.0.0.1")

    def test_dashboard_page_served(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Popularity tier list", body)
        self.assertIn("default-src 'self'", dict(headers)["Content-Security-Policy"])

    def test_site_data(self):
        d = self.get_json("/data/site.json")
        self.assertEqual(d["mode"], "local")
        self.assertTrue(d["demo"])
        self.assertEqual(len(d["snapshots"]), 2)
        older, newer = d["snapshots"]
        self.assertEqual(set(older["u"]), {"top50", "top100", "r51-100"})
        self.assertEqual(set(newer["u"]), {"top50"})  # only covers ranks 1-50: nothing beyond is offered
        self.assertEqual(newer["u"]["top50"]["n"], 50)
        self.assertEqual(newer["u"]["top50"]["c"]["junior"], 40)
        self.assertEqual(older["u"]["r51-100"]["c"].get("junior", 0), 0)
        self.assertIn("junior", d["creatures"])
        self.assertTrue(all(any(k in s["u"][r]["c"] for s in d["snapshots"] for r in s["u"]) for k in d["creatures"]))

    def test_nothing_personal_is_served(self):
        body = self.request("GET", "/data/site.json")[2] + self.request("GET", "/api/status")[2]
        for secret in ("jwa-test-", os.environ.get("USERNAME") or "no-user-name-set", "AppData", "collector.log"):
            self.assertNotIn(secret.encode("utf-8"), body)
        status = self.get_json("/api/status")
        self.assertEqual(set(status), {"latest_snapshot_id", "last_run", "collection_running", "demo"})

    def test_site_data_is_cached_until_something_changes(self):
        first = self.request("GET", "/data/site.json")[2]
        self.assertEqual(self.srv._cache[1], first)
        ingest.store(self.conn, ingest.prepare(snapshot([full_team(1)], snap_id="c", captured_at=T0 + hours(24))), now=T0)
        third = json.loads(self.request("GET", "/data/site.json")[2])
        self.assertEqual(len(third["snapshots"]), 3)

    def test_unknown_api_is_404(self):
        for path in ("/api/tierlist", "/api/nope"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_rejects_foreign_host_header(self):
        status, _, _ = self.request("GET", "/data/site.json", host="evil.example:80")
        self.assertEqual(status, 400)

    def test_post_requires_custom_header_and_same_origin(self):
        status, _, _ = self.request("POST", "/api/collect")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/collect", headers={"X-JWA-Tracker": "1", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, body = self.request("POST", "/api/collect", headers={"X-JWA-Tracker": "1"})
        self.assertEqual(status, 409)  # demo server refuses to collect
        self.assertIn(b"example data", body)

    def test_no_path_traversal_or_private_files(self):
        for path in ("/../tracker.py", "/%2e%2e/tracker.py", "/..%5ctracker.py", "/img/../../jwa_tracker/db.py",
                     "/img/silhouettes/credits.json"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)


class CsvImportTests(TempDataDir):
    def write_csv(self, rows):
        path = Path(tempfile.mkdtemp()) / "board.csv"
        header = "rank,trophies,player," + ",".join(f"creature{i}" for i in range(1, 9))
        path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_500_players_with_duplicates_and_missing(self):
        names = ["Becklerizaurus", "Shantrospinos", "Baryotor", "Atrocomaxima", "Titanotholus", "Becklejara",
                 "Junior", "Proto Lux"]
        rows = [f"{r},{7000 - r},Player{r}," + ",".join(names) for r in range(1, 501)]
        rows.append("12,6900,player5," + ",".join(names))       # duplicate player (case-insensitive)
        rows.append("501,1,Someone," + ",".join(names[:7]) + ",")  # only 7 creatures
        snap = csv_import.parse_csv(self.write_csv(rows), T0)
        prepared = ingest.prepare(snap)
        self.assertEqual(prepared.duplicates_dropped, 1)
        self.assertEqual(prepared.valid_count, 500)
        self.assertEqual(prepared.invalid_count, 1)
        self.assertEqual(prepared.rank_coverage_max, 500)
        ingest.store(self.conn, prepared, now=T0)
        from jwa_tracker import analysis

        a = analysis.Analyzer(self.conn)
        top500 = a.tierlist("arena", "top500", "latest")
        self.assertEqual(top500["sample"]["state"], "complete")
        self.assertEqual(top500["sample"]["teams"], 500)
        br = a.tierlist("arena", "r251-500", "latest")
        self.assertEqual(br["sample"]["teams"], 250)
        self.assertEqual({c["key"]: c for c in br["creatures"]}["junior"]["pct"], 100.0)
        trophies = self.conn.execute("SELECT trophies FROM teams WHERE rank_min = 1").fetchone()[0]
        self.assertEqual(trophies, 6999)

    def test_missing_columns(self):
        path = Path(tempfile.mkdtemp()) / "bad.csv"
        path.write_text("rank,creature1\n1,Junior\n", encoding="utf-8")
        from jwa_tracker.sources.base import SourceFormatError

        with self.assertRaises(SourceFormatError):
            csv_import.parse_csv(path, T0)


class VersionTests(TempDataDir):
    def test_minor_version(self):
        self.assertEqual(versions.minor_version("3.23.44"), "v3.23")
        self.assertIsNone(versions.minor_version("abc"))

    def test_timeline(self):
        tl = versions.VersionTimeline.load(self.conn)
        self.assertEqual(tl.version_at(datetime(2026, 9, 28, tzinfo=timezone.utc)), "v3.22")
        self.assertEqual(tl.version_at(datetime(2026, 10, 1, tzinfo=timezone.utc)), "v3.23")
        self.assertIsNone(tl.version_at(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    def test_new_version_recorded_once(self):
        released = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
        with db.transaction(self.conn):
            self.assertEqual(versions.record_build(self.conn, "3.24.10", released, released), "v3.24")
            self.assertIsNone(versions.record_build(self.conn, "3.24.12", released, released))
        tl = versions.VersionTimeline.load(self.conn)
        self.assertEqual(tl.version_at(datetime(2026, 11, 4, tzinfo=timezone.utc)), "v3.24")


class DemoIsolationTests(TempDataDir):
    def test_example_data_never_touches_real_db(self):
        from jwa_tracker import demo

        path = Path(tempfile.mkdtemp()) / "example.db"
        demo.build_demo_db(path, snapshots=2, players=500)
        self.assertEqual(db.list_snapshots(self.conn), [])  # real database untouched
        demo_conn = db.connect(path)
        snaps = db.list_snapshots(demo_conn)
        demo_conn.close()
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(s["is_example"] == 1 for s in snaps))
        self.assertTrue(all(s["teams_valid"] == 500 for s in snaps))


if __name__ == "__main__":
    unittest.main()
