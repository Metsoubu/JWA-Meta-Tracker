"""One collection run: check versions and roster, then import every new snapshot.

Safe to run at any time: a file lock prevents overlapping runs, each snapshot is
imported in its own transaction, and nothing already stored is ever modified.
If the source cannot be reached the run is logged as failed and the existing
data is left exactly as it was.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import config, db, images, ingest, net, roster, standins, versions
from .sources import default_sources
from .sources.base import SourceFormatError

log = logging.getLogger(__name__)


# --- locking ---------------------------------------------------------------------

class CollectorLock:
    """An OS-level file lock; released automatically if the process dies."""

    def __init__(self, path=None):
        self.path = path or config.lock_file()
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def is_collection_running() -> bool:
    lock = CollectorLock()
    if lock.acquire():
        lock.release()
        return False
    return True


# --- scheduling gate ---------------------------------------------------------------

def most_recent_slot(now: datetime) -> datetime:
    """The latest scheduled slot (local time) at or before `now`, returned in UTC."""
    local = now.astimezone()
    candidates = []
    for day_offset in (0, -1):
        day = local + timedelta(days=day_offset)
        for slot in config.SCHEDULE_SLOTS_LOCAL:
            hour, minute = (int(x) for x in slot.split(":"))
            candidates.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0))
    past = [c for c in candidates if c <= local]
    return max(past).astimezone(timezone.utc)


def next_slot(now: datetime) -> datetime:
    local = now.astimezone()
    candidates = []
    for day_offset in (0, 1):
        day = local + timedelta(days=day_offset)
        for slot in config.SCHEDULE_SLOTS_LOCAL:
            hour, minute = (int(x) for x in slot.split(":"))
            candidates.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0))
    return min(c for c in candidates if c > local).astimezone(timezone.utc)


def is_due(conn: sqlite3.Connection, now: datetime) -> tuple[bool, str]:
    last_ok = db.last_run(conn, db.SUCCESS_STATUSES)
    if not last_ok:
        return True, "no successful update yet"
    last_time = db.parse_iso(last_ok["started_at"])
    slot = most_recent_slot(now)
    if last_time < slot:
        return True, f"scheduled update for {slot.astimezone():%Y-%m-%d %H:%M} has not run yet"
    return False, f"already checked at {last_time.astimezone():%Y-%m-%d %H:%M}"


# --- the run -------------------------------------------------------------------------

@dataclass
class RunResult:
    status: str
    run_id: int | None = None
    new_snapshots: int = 0
    failed_snapshots: int = 0
    message: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "failed" else 0


def run_collection(
    trigger: str = "manual",
    *,
    force: bool = False,
    sources=None,
    conn: sqlite3.Connection | None = None,
    now_fn: Callable[[], datetime] = db.utcnow,
    roster_fetch: Callable[..., bytes] = net.fetch,
    version_fetch: Callable[[], tuple[str, datetime]] | None = None,
    fetch_images: bool = True,
    skip_ids: dict[str, set[str]] | None = None,
) -> RunResult:
    """Run one update. `skip_ids` (source -> snapshot ids) are treated as already saved elsewhere."""
    lock = CollectorLock()
    if not lock.acquire():
        log.info("Another update is already running; nothing to do.")
        return RunResult(status="skipped", message="Another update is already running.")
    own_conn = conn is None
    conn = conn or db.connect()
    try:
        now = now_fn()
        roster.ensure_roster(conn, now)
        if conn.execute("SELECT COUNT(*) FROM game_versions").fetchone()[0] == 0:
            with db.transaction(conn):
                versions.seed_known_versions(conn, now)
        if not force:
            due, reason = is_due(conn, now)
            if not due:
                log.info("No update needed: %s.", reason)
                return RunResult(status="skipped", message=f"No update needed ({reason}).")
        db.mark_interrupted_runs(conn, now)
        run_id = db.start_run(conn, trigger, now)
        log.info("Update started (trigger: %s).", trigger)
        result = _collect(conn, run_id, now_fn, sources or default_sources(), roster_fetch, version_fetch,
                          fetch_images, skip_ids or {})
        db.finish_run(
            conn, run_id, result.status, new_snapshots=result.new_snapshots,
            failed_snapshots=result.failed_snapshots, message=result.message, error=result.error,
            now=now_fn(),
        )
        log_fn = log.error if result.status == "failed" else log.info
        log_fn("Update finished: %s. %s %s", result.status, result.message, result.error)
        return result
    finally:
        if own_conn:
            conn.close()
        lock.release()


def _collect(conn, run_id, now_fn, sources, roster_fetch, version_fetch, fetch_images, skip_ids) -> RunResult:
    notes: list[str] = []
    now = now_fn()

    # 1. Game version (helpful, not essential)
    try:
        build, released = (version_fetch or versions.fetch_app_store_version)()
        with db.transaction(conn):
            new_version = versions.record_build(conn, build, released, now)
        notes.append(f"Game version {build}" + (f" (new version {new_version} recorded)" if new_version else "") + ".")
    except Exception as exc:  # noqa: BLE001 - optional step, report and continue
        notes.append(f"Could not check the game version ({exc}).")
        log.warning("Game version check failed: %s", exc)

    # 2. Creature roster (helpful, not essential)
    try:
        notes.append(roster.refresh_roster_if_due(conn, now, fetch=roster_fetch))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Could not refresh the creature roster; using the saved copy ({exc}).")
        log.warning("Roster refresh failed: %s", exc)

    if fetch_images:
        try:
            added = images.fetch_missing_silhouettes(conn, limit=25)
            if added:
                notes.append(f"Added {added} new creature silhouette(s).")
        except Exception as exc:  # noqa: BLE001
            log.warning("Silhouette update failed: %s", exc)

    # 3. Leaderboard snapshots (essential)
    imported = 0
    failures: list[str] = []
    newest_seen: datetime | None = None
    reached_any = False
    for source in sources:
        try:
            refs = source.list_snapshots()
            reached_any = True
        except (net.FetchError, SourceFormatError) as exc:
            failures.append(f"{source.label}: {exc}")
            log.error("Could not list snapshots from %s: %s", source.label, exc)
            continue
        if refs:
            newest_seen = max([newest_seen or refs[-1].captured_at, refs[-1].captured_at])
        known = (db.known_snapshot_ids(conn, source.name) | db.rejected_snapshot_ids(conn, source.name)
                 | set(skip_ids.get(source.name, ())))
        new_refs = [r for r in refs if r.id not in known]
        log.info("%s lists %d snapshots; %d are new.", source.label, len(refs), len(new_refs))
        for ref in new_refs:
            try:
                snapshot, raw = source.fetch_snapshot(ref)
                prepared = ingest.prepare(snapshot)
                ingest.store(conn, prepared, now=now_fn(), run_id=run_id, raw=raw)
                imported += 1
                log.info(
                    "Imported %s (%s, %d valid teams, ranks 1-%d).",
                    ref.id, snapshot.leaderboard_name, prepared.valid_count, prepared.rank_coverage_max,
                )
            except ingest.DuplicateSnapshot:
                continue
            except net.FetchError as exc:
                failures.append(f"{ref.id}: {exc}")
                log.error("Could not download snapshot %s: %s", ref.id, exc)
                if not exc.transient:
                    db.reject_snapshot(conn, source.name, ref.id, str(exc), now_fn())
            except (SourceFormatError, ValueError) as exc:
                # The file itself is unusable; record it so it is not retried every hour.
                failures.append(f"{ref.id}: {exc}")
                log.error("Rejected snapshot %s: %s", ref.id, exc)
                db.reject_snapshot(conn, source.name, ref.id, str(exc), now_fn())

    if fetch_images and imported:
        try:
            added = standins.update(conn, standins.used_creature_keys(conn), limit=10)
            if added:
                notes.append(f"Found body-type icons for {added} new creature(s).")
        except Exception as exc:  # noqa: BLE001 - pictures are optional
            log.warning("Body-type icon update failed: %s", exc)

    if newest_seen and now - newest_seen > timedelta(hours=config.STALE_AFTER_HOURS):
        notes.append(
            f"The data source has not published anything new since {newest_seen.astimezone():%Y-%m-%d %H:%M}."
        )

    if not reached_any:
        status = "failed"
        message = "Could not reach the data source. Existing data was kept unchanged."
    elif imported and not failures:
        status = "success"
        message = f"Imported {imported} new snapshot(s)."
    elif imported:
        status = "partial"
        message = f"Imported {imported} new snapshot(s); {len(failures)} could not be imported."
    elif failures:
        status = "failed"
        message = f"{len(failures)} new snapshot(s) could not be imported. Existing data was kept unchanged."
    else:
        status = "no_new_data"
        message = "No new snapshots yet; everything the source has published is already saved."
    if notes:
        message = f"{message} {' '.join(notes)}"
    return RunResult(
        status=status,
        run_id=run_id,
        new_snapshots=imported,
        failed_snapshots=len(failures),
        message=message.strip(),
        error="\n".join(failures),
        notes=notes,
    )
