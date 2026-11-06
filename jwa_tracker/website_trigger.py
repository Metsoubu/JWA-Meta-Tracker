"""On the owner's PC: ask GitHub to update the public website when a new tournament snapshot appears.

GitHub starts its own timers late or skips them (2-5 hours late in November 2026), so
the PC, which checks the feed every few minutes anyway, is a second way to start an
update. It uses the login the GitHub CLI already has; nothing is stored here. On any
other computer (no git copy of this project, or no GitHub CLI, as with the share zip)
it does nothing. A request the website did not need only causes a quick, harmless rebuild.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess

from . import config, db, net, scheduler
from .sources import default_sources, jwa_dashboard_feed
from .sources.base import SourceFormatError

log = logging.getLogger(__name__)

WORKFLOW = "update-website.yml"
LAST_ASKED = "website_asked_for_snapshot"


def newest_tournament_snapshot(sources) -> str | None:
    refs = []
    for source in sources:
        refs += [ref for ref in source.list_snapshots() if jwa_dashboard_feed.is_tournament_ref(ref)]
    return max(refs, key=lambda ref: ref.captured_at).id if refs else None


def ask_github(conn: sqlite3.Connection, sources=None) -> bool:
    """Start one website update per new tournament snapshot. True if one was started.

    A request that fails is tried again on the next check. Nothing here can affect the PC's own update."""
    gh = shutil.which("gh")
    if not gh or not (config.PROJECT_DIR / ".git").exists():
        return False
    try:
        newest = newest_tournament_snapshot(sources if sources is not None else default_sources())
        if newest is None or newest == db.get_meta(conn, LAST_ASKED):
            return False
        result = subprocess.run(
            [gh, "workflow", "run", WORKFLOW, "--ref", "main"], cwd=config.PROJECT_DIR, capture_output=True, text=True,
            timeout=60, creationflags=scheduler.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            log.warning("Could not ask GitHub to update the website: %s", (result.stderr or result.stdout).strip()[:300])
            return False
        with db.transaction(conn):
            db.set_meta(conn, LAST_ASKED, newest)
    except (net.FetchError, SourceFormatError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        log.warning("Could not ask GitHub to update the website: %s", exc)
        return False
    log.info("Asked GitHub to update the website for tournament snapshot %s.", newest)
    return True
