"""Feed parsing (real fixture), collection runs, failures, retries and scheduling gate."""
import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from jwa_tracker import collector, db, net
from jwa_tracker.sources import jwa_dashboard_feed as feed
from jwa_tracker.sources.base import SnapshotRef, SourceFormatError
from tests.helpers import FIXTURES, T0, FakeSource, TempDataDir, full_team, hours, snapshot

REAL_FIXTURE = FIXTURES / "real_feed_snapshot_2026-10-08_21-31-45.json"


class FeedParsingTests(unittest.TestCase):
    def test_parses_real_snapshot(self):
        snap = feed.parse_snapshot(REAL_FIXTURE.read_bytes(), "2026-10-08_21-31-45")
        self.assertEqual(snap.kind, "arena")
        self.assertEqual(len(snap.teams), 100)
        self.assertEqual(snap.captured_at, datetime(2026, 10, 8, 21, 31, 53, 128422, tzinfo=timezone.utc))
        self.assertTrue(all(len(t.members) == 8 for t in snap.teams))
        self.assertEqual({(t.rank_min, t.rank_max) for t in snap.teams}, {(lo, lo + 9) for lo in range(1, 100, 10)})
        self.assertIn("Seasonal League", snap.leaderboard_name)
        self.assertTrue(snap.fingerprint_identifies_player)

    def test_real_snapshot_imports_cleanly(self):
        from jwa_tracker import ingest

        prepared = ingest.prepare(feed.parse_snapshot(REAL_FIXTURE.read_bytes(), "x"))
        self.assertEqual(prepared.valid_count, 100)
        self.assertEqual(prepared.duplicates_dropped, 0)
        self.assertEqual(prepared.rank_coverage_max, 100)

    def test_unknown_format_refused(self):
        doc = json.loads(REAL_FIXTURE.read_bytes())
        doc["schema_version"] = "something-else.v9"
        with self.assertRaises(SourceFormatError):
            feed.parse_snapshot(json.dumps(doc).encode(), "x")

    def test_newer_version_of_same_format_parsed_with_note(self):
        doc = json.loads(REAL_FIXTURE.read_bytes())
        doc["schema_version"] = "jwa-dashboard.public.v3"
        snap = feed.parse_snapshot(json.dumps(doc).encode(), "x")
        self.assertTrue(any("newer" in n for n in snap.notes))

    def test_garbage_refused(self):
        for raw in (b"not json", b"[]", b'{"schema_version": "jwa-dashboard.public.v2"}'):
            with self.subTest(raw=raw), self.assertRaises(SourceFormatError):
                feed.parse_snapshot(raw, "x")

    def test_tournament_kind(self):
        doc = json.loads(REAL_FIXTURE.read_bytes())
        doc["event"] = {"name": "[Tournament] 2026-09-W5 - Coin Tournament", "is_arena": False}
        self.assertEqual(feed.parse_snapshot(json.dumps(doc).encode(), "x").kind, "tournament")

    def test_run_index(self):
        refs = feed.parse_run_index([
            {"snapshot_folder": "2026-10-08_21-31-45", "captured_at": "2026-10-08T21:31:53+00:00", "label": "a"},
            {"snapshot_folder": "../../etc", "captured_at": "2026-10-08T21:31:53+00:00"},
            {"snapshot_folder": "2026-10-07_11-21-59", "captured_at": "2026-10-07T11:22:06+00:00"},
            "junk",
        ])
        self.assertEqual([r.id for r in refs], ["2026-10-07_11-21-59", "2026-10-08_21-31-45"])
        with self.assertRaises(SourceFormatError):
            feed.parse_run_index({"not": "a list"})

    def test_adapter_refuses_unexpected_ids(self):
        src = feed.JwaDashboardFeed(fetch=lambda url, **k: b"", fetch_json=lambda url, **k: [])
        with self.assertRaises(SourceFormatError):
            src.fetch_snapshot(SnapshotRef(id="../secret", captured_at=T0))

    def test_adapter_builds_expected_urls(self):
        urls = []

        def fake_fetch(url, **k):
            urls.append(url)
            return REAL_FIXTURE.read_bytes()

        src = feed.JwaDashboardFeed(fetch=fake_fetch, fetch_json=lambda url, **k: [])
        src.fetch_snapshot(SnapshotRef(id="2026-10-08_21-31-45", captured_at=T0))
        self.assertEqual(urls, [f"{feed.RAW_BASE}/2026-10-08_21-31-45/dashboard-data.json"])


def fake_version():
    return "3.23.44", datetime(2026, 9, 29, 15, 8, 22, tzinfo=timezone.utc)


def no_roster(*a, **k):
    raise net.FetchError("offline in tests", transient=True)


class CollectorTests(TempDataDir):
    def run_collection(self, source, trigger="manual", force=True, now=None):
        return collector.run_collection(
            trigger, force=force, sources=[source], conn=self.conn, now_fn=lambda: now or T0 + hours(30),
            roster_fetch=no_roster, version_fetch=fake_version, fetch_images=False,
        )

    def two_snapshots(self):
        return [snapshot([full_team(r) for r in range(1, 11)], snap_id="a", captured_at=T0, source="test-source"),
                snapshot([full_team(r) for r in range(1, 11)], snap_id="b", captured_at=T0 + hours(12), source="test-source")]

    def test_success_then_no_new_data(self):
        src = FakeSource(self.two_snapshots())
        r1 = self.run_collection(src)
        self.assertEqual((r1.status, r1.new_snapshots, r1.exit_code), ("success", 2, 0))
        self.assertIn("Game version 3.23.44", r1.message)
        r2 = self.run_collection(src)
        self.assertEqual((r2.status, r2.new_snapshots), ("no_new_data", 0))
        self.assertEqual(len(db.list_snapshots(self.conn)), 2)
        runs = db.recent_runs(self.conn)
        self.assertEqual([r["status"] for r in runs], ["no_new_data", "success"])

    def test_unreachable_source_keeps_existing_data(self):
        self.run_collection(FakeSource(self.two_snapshots()))
        before = self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        broken = FakeSource(list_error=net.FetchError("HTTP 503", transient=True))
        r = self.run_collection(broken)
        self.assertEqual((r.status, r.exit_code), ("failed", 1))
        self.assertIn("kept unchanged", r.message)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0], before)
        self.assertEqual(db.consecutive_failures(self.conn), 1)

    def test_temporary_download_failure_is_retried_next_run(self):
        snaps = self.two_snapshots()
        src = FakeSource(snaps, fetch_errors={"b": net.FetchError("timeout", transient=True)})
        r = self.run_collection(src)
        self.assertEqual((r.status, r.new_snapshots, r.failed_snapshots), ("partial", 1, 1))
        src.fetch_errors = {}
        r2 = self.run_collection(src)
        self.assertEqual((r2.status, r2.new_snapshots), ("success", 1))

    def test_broken_snapshot_rejected_once_not_retried_forever(self):
        src = FakeSource(self.two_snapshots(), fetch_errors={"b": SourceFormatError("bad file")})
        r = self.run_collection(src)
        self.assertEqual(r.status, "partial")
        self.assertEqual([x["source_snapshot_id"] for x in db.rejected_snapshots(self.conn)], ["b"])
        src.fetched.clear()
        r2 = self.run_collection(src)
        self.assertEqual(r2.status, "no_new_data")
        self.assertNotIn("b", src.fetched)

    def test_never_substitutes_data_when_everything_fails(self):
        src = FakeSource(self.two_snapshots(), fetch_errors={"a": SourceFormatError("x"), "b": SourceFormatError("y")})
        r = self.run_collection(src)
        self.assertEqual(r.status, "failed")
        self.assertEqual(db.list_snapshots(self.conn), [])

    def test_lock_prevents_overlapping_runs(self):
        lock = collector.CollectorLock()
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(collector.is_collection_running())
            r = self.run_collection(FakeSource(self.two_snapshots()))
            self.assertEqual(r.status, "skipped")
        finally:
            lock.release()
        self.assertFalse(collector.is_collection_running())

    def test_interrupted_runs_are_marked(self):
        db.start_run(self.conn, "scheduled", T0)
        self.run_collection(FakeSource(self.two_snapshots()))
        statuses = [r["status"] for r in db.recent_runs(self.conn)]
        self.assertEqual(statuses, ["success", "interrupted"])

    def test_scheduled_gate(self):
        src = FakeSource(self.two_snapshots())
        local = datetime(2026, 10, 9, 9, 0).astimezone()  # 09:00 local: the 08:00 slot has passed
        first = self.run_collection(src, trigger="scheduled", force=False, now=local.astimezone(timezone.utc))
        self.assertEqual(first.status, "success")
        again = self.run_collection(src, trigger="scheduled", force=False,
                                    now=datetime(2026, 10, 9, 10, 0).astimezone().astimezone(timezone.utc))
        self.assertEqual(again.status, "skipped")  # already checked since 08:00
        evening = self.run_collection(src, trigger="scheduled", force=False,
                                      now=datetime(2026, 10, 9, 20, 30).astimezone().astimezone(timezone.utc))
        self.assertEqual(evening.status, "no_new_data")  # 20:00 slot due -> runs
        # a computer that was off for two days catches up on the next hourly check
        later = self.run_collection(src, trigger="scheduled", force=False,
                                    now=datetime(2026, 10, 11, 14, 0).astimezone().astimezone(timezone.utc))
        self.assertEqual(later.status, "no_new_data")

    def test_failure_makes_next_hourly_check_retry(self):
        broken = FakeSource(list_error=net.FetchError("down", transient=True))
        t = datetime(2026, 10, 9, 9, 0).astimezone().astimezone(timezone.utc)
        self.assertEqual(self.run_collection(broken, trigger="scheduled", force=False, now=t).status, "failed")
        due, _ = collector.is_due(self.conn, t + hours(1))
        self.assertTrue(due)

    def test_slot_maths(self):
        now = datetime(2026, 10, 9, 7, 30).astimezone()
        self.assertEqual(collector.most_recent_slot(now).astimezone().hour, 20)  # yesterday 20:00
        self.assertEqual(collector.next_slot(now).astimezone().hour, 8)


class _FlakyHandler(BaseHTTPRequestHandler):
    calls = 0
    mode = "flaky"

    def do_GET(self):  # noqa: N802
        type(self).calls += 1
        if self.mode == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if self.calls <= 2:
            self.send_response(503)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RetryTests(unittest.TestCase):
    def setUp(self):
        _FlakyHandler.calls = 0
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/x"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_retries_temporary_errors_then_succeeds(self):
        _FlakyHandler.mode = "flaky"
        sleeps = []
        data = net.fetch_json(self.url, sleep=sleeps.append, backoff=(1, 2, 3))
        self.assertEqual(data, {"ok": True})
        self.assertEqual(_FlakyHandler.calls, 3)
        self.assertEqual(sleeps, [1, 2])

    def test_gives_up_after_attempts(self):
        _FlakyHandler.mode = "flaky"
        with self.assertRaises(net.FetchError) as ctx:
            net.fetch(self.url, attempts=2, sleep=lambda s: None)
        self.assertTrue(ctx.exception.transient)

    def test_permanent_errors_not_retried(self):
        _FlakyHandler.mode = "missing"
        with self.assertRaises(net.FetchError) as ctx:
            net.fetch(self.url, sleep=lambda s: None)
        self.assertFalse(ctx.exception.transient)
        self.assertEqual(_FlakyHandler.calls, 1)

    def test_unreachable_host_is_transient(self):
        with self.assertRaises(net.FetchError) as ctx:
            net.fetch("http://127.0.0.1:9/", attempts=1, timeout=2)
        self.assertTrue(ctx.exception.transient)


if __name__ == "__main__":
    unittest.main()
