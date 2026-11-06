"""Tournament snapshots reach the website and the PC without waiting for the 6-hour schedule (synthetic data)."""
import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jwa_tracker import cli, collector, db, ingest, net, site
from tests.helpers import T0, FakeSource, TempDataDir, full_team, hours, snapshot
from tests.test_site import fake_version, no_roster
from jwa_tracker.sources.base import SnapshotRef


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class ListedSource:
    """A feed that only answers with its list of snapshots."""

    name = "jwa-dashboard-feed"
    label = "Synthetic feed"

    def __init__(self, *refs, error=None):
        self.refs = list(refs)
        self.error = error

    def list_snapshots(self):
        if self.error:
            raise self.error
        return self.refs


def tournament_ref(snap_id, when):
    return SnapshotRef(id=snap_id, captured_at=when, label="[Tournament] 2026-11-W1 - Cash Tournament")


def arena_ref(snap_id, when):
    return SnapshotRef(id=snap_id, captured_at=when, label="[Seasonal] 2026-11 - Unique Mutadon Seasonal League")


class CentralTimeTests(TempDataDir):
    def test_daylight_saving_switches_on_the_right_days(self):
        self.assertEqual(site.central_utc_offset(utc(2026, 7, 1)), timedelta(hours=-5))
        self.assertEqual(site.central_utc_offset(utc(2026, 11, 6)), timedelta(hours=-6))
        self.assertEqual(site.central_utc_offset(utc(2026, 1, 15)), timedelta(hours=-6))
        self.assertEqual(site.central_utc_offset(utc(2026, 3, 8, 7, 59)), timedelta(hours=-6))  # 2nd Sunday of March
        self.assertEqual(site.central_utc_offset(utc(2026, 3, 8, 8, 0)), timedelta(hours=-5))
        self.assertEqual(site.central_utc_offset(utc(2026, 11, 1, 6, 59)), timedelta(hours=-5))  # 1st Sunday of November
        self.assertEqual(site.central_utc_offset(utc(2026, 11, 1, 7, 0)), timedelta(hours=-6))

    def test_last_slot(self):
        self.assertEqual(site.last_central_slot(utc(2026, 11, 6, 20, 13)), utc(2026, 11, 6, 18))  # 2:13 pm CST -> noon
        self.assertEqual(site.last_central_slot(utc(2026, 11, 7, 0, 0)), utc(2026, 11, 7, 0))     # 6 pm CST exactly
        self.assertEqual(site.last_central_slot(utc(2026, 7, 1, 4, 30)), utc(2026, 6, 30, 23))    # 11:30 pm CDT -> 6 pm
        self.assertEqual(site.last_central_slot(utc(2026, 7, 1, 5, 30)), utc(2026, 7, 1, 5))      # 12:30 am CDT -> midnight


class WebsiteDueTests(TempDataDir):
    NOW = utc(2026, 11, 6, 20, 13)  # 2:13 pm Central; the noon slot was 18:00 UTC

    def archive(self, last_build=None, snapshot_ids=()):
        folder = Path(tempfile.mkdtemp())
        if last_build:
            (folder / "runs.json").write_text(json.dumps([{"started_at": db.iso(last_build), "status": "success"}]))
        (folder / "snapshots").mkdir()
        recs = [{"id": f"jwa-dashboard-feed:{sid}"} for sid in snapshot_ids]
        (folder / "snapshots" / "2026-11-06.json").write_text(json.dumps(recs))
        return folder

    def test_first_ever_build(self):
        self.assertTrue(site.website_due(self.archive(), self.NOW, [ListedSource()])[0])

    def test_catches_up_on_a_late_or_skipped_slot(self):
        due, why = site.website_due(self.archive(last_build=utc(2026, 11, 6, 13)), self.NOW, [ListedSource()])
        self.assertTrue(due)
        self.assertIn("2026-11-06T18:00", why)

    def test_nothing_to_do_between_slots(self):
        source = ListedSource(tournament_ref("old", utc(2026, 11, 2, 13)), arena_ref("a", utc(2026, 11, 6, 13)))
        due, _ = site.website_due(self.archive(last_build=utc(2026, 11, 6, 19), snapshot_ids=["old", "a"]), self.NOW, [source])
        self.assertFalse(due)

    def test_new_tournament_snapshot_is_built_straight_away(self):
        source = ListedSource(tournament_ref("old", utc(2026, 11, 2, 13)), tournament_ref("new", utc(2026, 11, 6, 20)))
        due, why = site.website_due(self.archive(last_build=utc(2026, 11, 6, 19), snapshot_ids=["old"]), self.NOW, [source])
        self.assertTrue(due)
        self.assertIn("new", why)

    def test_new_arena_snapshot_waits_for_the_schedule(self):
        source = ListedSource(arena_ref("new", utc(2026, 11, 6, 20)))
        due, _ = site.website_due(self.archive(last_build=utc(2026, 11, 6, 19)), self.NOW, [source])
        self.assertFalse(due)

    def test_snapshot_published_during_a_build_is_not_missed(self):
        source = ListedSource(tournament_ref("new", utc(2026, 11, 6, 19, 55)))
        due, _ = site.website_due(self.archive(last_build=utc(2026, 11, 6, 20)), self.NOW, [source])
        self.assertTrue(due)

    def test_a_snapshot_that_cannot_be_imported_does_not_rebuild_forever(self):
        source = ListedSource(tournament_ref("broken", utc(2026, 11, 6, 18, 30)))
        due, _ = site.website_due(self.archive(last_build=utc(2026, 11, 6, 20)), self.NOW, [source])
        self.assertFalse(due)  # builds have moved past it; the next 6-hour update tries again

    def test_feed_down_means_no_rebuild_and_no_crash(self):
        source = ListedSource(error=net.FetchError("down", transient=True))
        due, why = site.website_due(self.archive(last_build=utc(2026, 11, 6, 19)), self.NOW, [source])
        self.assertFalse(due)
        self.assertIn("could not check", why)

    def test_answer_is_handed_to_github_actions(self):
        answer = Path(tempfile.mkdtemp()) / "output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(answer)}), \
                mock.patch("jwa_tracker.site.website_due", return_value=(True, "because")):
            cli.cmd_website_due(argparse.Namespace(archive="archive"))
        self.assertEqual(answer.read_text(encoding="utf-8"), "run=yes\n")


class PcTournamentTests(TempDataDir):
    def source(self, label):
        snap = snapshot([full_team(r) for r in range(1, 51)], snap_id="2026-11-06_20-00-00", kind="tournament",
                        captured_at=T0 + hours(1))

        class Labelled(FakeSource):
            def list_snapshots(self):
                return [SnapshotRef(id=r.id, captured_at=r.captured_at, label=label) for r in super().list_snapshots()]

        return Labelled([snap])

    def checked_just_now(self):
        now = datetime(2026, 10, 9, 9, 0).astimezone().astimezone(timezone.utc)
        run = db.start_run(self.conn, "scheduled", now)
        db.finish_run(self.conn, run, "success", now=now)
        return now + hours(1)  # 10 am: the next 6-hour slot is still two hours away

    def collect(self, source, now):
        return collector.run_collection("scheduled", force=False, sources=[source], conn=self.conn, now_fn=lambda: now,
                                        roster_fetch=no_roster, version_fetch=fake_version, fetch_images=False)

    def test_hourly_check_imports_a_new_tournament_snapshot_right_away(self):
        result = self.collect(self.source("[Tournament] 2026-11-W1 - Cash Tournament"), self.checked_just_now())
        self.assertEqual((result.status, result.new_snapshots), ("success", 1))

    def test_arena_snapshots_wait_for_the_schedule(self):
        result = self.collect(self.source("[Seasonal] 2026-11 - Unique Mutadon"), self.checked_just_now())
        self.assertEqual(result.status, "skipped")

    def test_saved_or_rejected_tournament_snapshots_do_not_trigger_updates(self):
        now = self.checked_just_now()
        source = self.source("[Tournament] 2026-11-W1 - Cash Tournament")
        db.reject_snapshot(self.conn, source.name, "2026-11-06_20-00-00", "unusable", now)
        self.assertEqual(self.collect(source, now).status, "skipped")

    def test_feed_down_is_not_an_update(self):
        source = FakeSource(list_error=net.FetchError("down", transient=True))
        self.assertEqual(self.collect(source, self.checked_just_now()).status, "skipped")
