"""Command-line entry points (used by the .bat files and Task Scheduler)."""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import collector, config, db, logsetup, roster, scheduler, server, versions

log = logging.getLogger("jwa_tracker")


def _say(text: str = "") -> None:
    if sys.stdout is not None:
        try:
            print(text, flush=True)
        except (OSError, UnicodeEncodeError):
            pass


def _prepare_db():
    conn = db.connect()
    now = db.utcnow()
    roster.ensure_roster(conn, now)
    if conn.execute("SELECT COUNT(*) FROM game_versions").fetchone()[0] == 0:
        with db.transaction(conn):
            versions.seed_known_versions(conn, now)
    return conn


def cmd_start(args: argparse.Namespace) -> int:
    _say(f"=== {config.APP_NAME} ===")
    conn = _prepare_db()
    _say(f"Data folder: {config.data_dir()}")

    if not args.no_schedule:
        ok, message = scheduler.ensure_installed()
        _say(("[ok] " if ok else "[!] ") + message)
        if not ok:
            _say("    The dashboard still works; use UPDATE_NOW.bat to fetch data manually.")

    due, reason = collector.is_due(conn, db.utcnow())
    conn.close()
    if due and not collector.is_collection_running():
        server.spawn_background_collection("startup")
        _say(f"[..] Fetching the latest leaderboard data in the background ({reason}).")
    else:
        _say(f"[ok] Data is up to date ({reason}).")

    for port in range(config.DEFAULT_PORT, config.DEFAULT_PORT + config.PORT_SEARCH_RANGE):
        if server.probe(port) and not server.probe(port).get("demo"):
            url = f"http://127.0.0.1:{port}/"
            _say(f"[ok] The dashboard is already running at {url} - opening it.")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
    return _serve(config.db_path(), demo=False, open_browser=not args.no_browser)


def _serve(db_file: Path, demo: bool, open_browser: bool, preferred: int = config.DEFAULT_PORT) -> int:
    srv = server.make_server(db_file, demo=demo, preferred=preferred)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    _say("")
    _say(f"Dashboard running at {url}  (only reachable from this computer)")
    _say("Keep this window open while you use the dashboard. Close it to stop the dashboard.")
    _say("Automatic updates keep running in the background even when this window is closed.")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    _prepare_db().close()
    return _serve(config.db_path(), demo=False, open_browser=not args.no_browser, preferred=args.port)


def cmd_collect(args: argparse.Namespace) -> int:
    force = args.force or args.trigger == "manual"
    result = collector.run_collection(trigger=args.trigger, force=force)
    _say(f"Result: {result.status}. {result.message}")
    if result.error:
        _say("Problems:\n  " + result.error.replace("\n", "\n  "))
    return result.exit_code


def cmd_schedule(args: argparse.Namespace) -> int:
    if args.action == "install":
        ok, message = scheduler.install()
    elif args.action == "remove":
        ok, message = scheduler.remove()
    else:
        info = scheduler.status(max_age=0)
        for k, v in info.items():
            _say(f"{k:12s} {v}")
        return 0 if info.get("installed") else 1
    _say(message)
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    conn = _prepare_db()
    latest = db.latest_snapshot(conn, "arena")
    last = db.last_run(conn)
    _say(f"Database:       {config.db_path()}")
    _say(f"Snapshots:      {len(db.list_snapshots(conn))} total, {len(db.list_snapshots(conn, 'arena'))} arena")
    if latest:
        _say(f"Newest arena:   {latest['captured_at']}  {latest['leaderboard_name']}  ({latest['teams_valid']} teams)")
    if last:
        _say(f"Last update:    {last['started_at']}  {last['status']}  {last['message'] or ''}")
        if last.get("error"):
            _say(f"Last error:     {last['error']}")
    due, reason = collector.is_due(conn, db.utcnow())
    _say(f"Next update:    {'due now' if due else 'not yet'} ({reason})")
    return 0


def cmd_import_csv(args: argparse.Namespace) -> int:
    from . import ingest
    from .sources import csv_import

    captured = datetime.fromisoformat(args.captured_at) if args.captured_at else datetime.now()
    if captured.tzinfo is None:
        captured = captured.astimezone()
    captured = captured.astimezone(timezone.utc)
    conn = _prepare_db()
    snap = csv_import.parse_csv(Path(args.file), captured, kind=args.kind, label=args.label or "")
    prepared = ingest.prepare(snap)
    try:
        snap_id = ingest.store(conn, prepared, now=db.utcnow(), raw=Path(args.file).read_bytes())
    except ingest.DuplicateSnapshot:
        _say("This file was already imported.")
        return 1
    _say(f"Imported snapshot #{snap_id}: {prepared.valid_count} valid teams, "
         f"{prepared.invalid_count} invalid, {prepared.duplicates_dropped} duplicates removed.")
    for note in prepared.notes:
        _say(f"  note: {note}")
    unmatched = conn.execute(
        "SELECT display_name FROM source_creatures WHERE source = 'csv-import' AND match_method = 'unmatched'"
    ).fetchall()
    if unmatched:
        _say("  Unrecognised creature names: " + ", ".join(r[0] for r in unmatched))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from . import demo

    _say("Building EXAMPLE DATA (randomly generated, not real) in a separate database...")
    path = demo.build_demo_db()
    return _serve(path, demo=True, open_browser=not args.no_browser, preferred=config.DEFAULT_PORT + 20)


def cmd_build_site(args: argparse.Namespace) -> int:
    from . import site

    result = site.build_website(Path(args.out), Path(args.archive), repo_url=site.repo_url_from_env())
    _say(f"Website written to {args.out}: {result['total']} snapshots ({result['added']} new). "
         f"Update: {result['status']}. {result['message']}")
    return 0 if result["total"] else 1


def cmd_fetch_images(args: argparse.Namespace) -> int:
    from . import images

    conn = _prepare_db()
    added = images.fetch_missing_silhouettes(conn, limit=None)
    _say(f"Added {added} silhouettes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker.py", description=config.APP_NAME)
    parser.add_argument("--data-dir", help="use a different data folder (for testing)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="set everything up, then open the dashboard")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--no-schedule", action="store_true")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("serve", help="run only the dashboard server")
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("collect", help="fetch new leaderboard data")
    p.add_argument("--trigger", choices=("manual", "scheduled", "startup"), default="manual")
    p.add_argument("--force", action="store_true", help="run even if no update is due")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("schedule", help="manage the Windows Task Scheduler entry")
    p.add_argument("action", choices=("install", "remove", "status"))
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("status", help="print a short status report")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("import-csv", help="import a leaderboard CSV you are permitted to use")
    p.add_argument("file")
    p.add_argument("--captured-at", help="when the leaderboard was recorded, e.g. 2026-10-08T20:00")
    p.add_argument("--kind", choices=("arena", "tournament", "other"), default="arena")
    p.add_argument("--label")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("demo", help="open the dashboard with EXAMPLE (fake) data for testing")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("fetch-images", help="look up openly licensed silhouettes for all creatures")
    p.set_defaults(func=cmd_fetch_images)

    p = sub.add_parser("build-site", help="update the archive and write the public website (used by GitHub Actions)")
    p.add_argument("--out", default="_site", help="folder to write the website into")
    p.add_argument("--archive", default="archive", help="folder holding the website's snapshot history")
    p.set_defaults(func=cmd_build_site)

    args = parser.parse_args(argv)
    import os

    if args.data_dir:
        os.environ["JWA_TRACKER_DATA_DIR"] = args.data_dir
    elif args.command == "build-site":
        import tempfile

        # Website builds use a throw-away database, never your personal one.
        os.environ["JWA_TRACKER_DATA_DIR"] = tempfile.mkdtemp(prefix="jwa-site-build-")
    logsetup.setup_logging(console=sys.stderr is not None)
    try:
        return int(args.func(args) or 0)
    except Exception:  # noqa: BLE001 - make sure every crash reaches the log file
        log.exception("Command %s failed", args.command)
        _say(f"Something went wrong. Details were written to {config.log_file()}")
        return 2
