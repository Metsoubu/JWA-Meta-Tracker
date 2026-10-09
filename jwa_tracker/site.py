"""Everything the dashboard shows, as one JSON file (data/site.json).

The dashboard (web/app.js) is a static page: it loads data/site.json and does
the pooling, tiers and comparisons in the browser. The same files therefore work
as your local dashboard (the local server builds site.json from your database)
and as a free public website on GitHub Pages, where a scheduled GitHub Action
runs `tracker.py build-site` twice a day.

For each snapshot the file holds, per rank range, the number of valid teams and
how many of them use each creature. Counting is done here, in tested Python; the
browser only adds counts together and divides.

Nothing personal is included: no file paths, user names or computer settings.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import collector, config, db, roster, standins, stats, versions
from .sources import jwa_dashboard_feed

log = logging.getLogger(__name__)

SCHEMA = "jwa-meta-tracker.site.v1"
# "#1-50" is the same set of players as Top 50, so it is not offered separately.
EXPORT_RANGES = ("top50", "top100", "r51-100", "top250", "r101-250", "top500", "r251-500")
WEB_FILES = ("index.html", "app.js", "styles.css", "theme.js", "favicon.svg")
SOURCES = {
    jwa_dashboard_feed.SOURCE_NAME: {"label": "jwa-dashboard community feed", "url": jwa_dashboard_feed.REPOSITORY_URL},
    "csv-import": {"label": "Imported CSV file", "url": None},
    "example": {"label": "EXAMPLE DATA (randomly generated, not real)", "url": None},
}
RUN_HISTORY = 60
_PATHLIKE = re.compile(r"(?:[A-Za-z]:\\|/(?:home|Users|tmp|var|private)/)[^\s'\"]*")


def sanitize(text: str | None) -> str:
    """Remove anything that looks like a local file path from a message."""
    return _PATHLIKE.sub("<file>", text or "").strip()


# --- per-snapshot records ---------------------------------------------------------

def snapshot_record(snap: dict[str, Any], teams: list[stats.Team]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in EXPORT_RANGES:
        lo, hi, _label = stats.rank_filter(key)
        if hi > snap["rank_coverage_max"]:
            continue  # the source does not cover this range: offer nothing rather than a partial guess
        inside, _ = stats.filter_by_rank(teams, lo, hi)
        counts, total = stats.count_usage(inside)
        usage[key] = {"n": total, "c": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))}
    return {
        "id": f"{snap['source']}:{snap['source_snapshot_id']}",
        "t": snap["captured_at"],
        "k": snap["kind"],
        "lb": snap["leaderboard_name"] or "",
        "cov": snap["rank_coverage_max"],
        "inv": snap["teams_invalid"],
        "src": snap["source"],
        "ex": int(snap["is_example"]),
        "u": usage,
    }


def snapshot_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    snaps = db.list_snapshots(conn)
    teams_by_snapshot: dict[int, list[stats.Team]] = defaultdict(list)
    for team in stats.teams_from_rows(db.load_team_rows(conn, [s["id"] for s in snaps])):
        teams_by_snapshot[team.snapshot_id].append(team)
    records = [snapshot_record(s, teams_by_snapshot[s["id"]]) for s in snaps]
    records.sort(key=lambda r: (r["t"], r["id"]))
    return records


def used_keys(records: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for r in records:
        for block in r["u"].values():
            keys.update(block["c"])
    return keys


def creature_info(conn: sqlite3.Connection, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    keys = sorted(set(keys))
    creatures = db.all_creatures(conn)
    pictures = standins.picture_index(keys)
    aka: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT creature_key, display_name FROM source_creatures"):
        if r["display_name"]:
            aka[r["creature_key"]].add(roster.clean_display_name(r["display_name"]))
    out = {}
    for key in keys:
        c = creatures.get(key)
        if not c:
            continue
        pic = pictures.get(key, {})
        out[key] = {
            "name": c["name"],
            "rarity": c["rarity"],
            "class": c["class"],
            "hybrid": c["hybrid_type"],
            "added": c["added_version"],
            "listed": bool(c["in_roster"]),
            "img": pic.get("url"),
            "imgKind": pic.get("kind"),
            "credit": pic.get("credit"),
            "aka": sorted(aka.get(key, ())),
        }
    return out


def run_entries(conn: sqlite3.Connection, limit: int = RUN_HISTORY) -> list[dict[str, Any]]:
    return [
        {
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "trigger": r["trigger"],
            "status": r["status"],
            "new_snapshots": r["new_snapshots"],
            "message": sanitize(r["message"]),
            "error": sanitize(r["error"]),
        }
        for r in db.recent_runs(conn, limit)
        if r["status"] != "running"
    ]


def credits_list(creatures: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    from . import images

    used_files = {c["img"] for c in creatures.values() if c.get("img")}
    out = []
    for key, c in images.load_credits().items():
        if c.get("status") == "ok" and f"img/silhouettes/{c.get('file')}" in used_files:
            out.append({"taxon": c.get("taxon", ""), "by": c.get("attribution") or "unknown",
                        "license": c.get("license_name", ""), "url": c.get("page", "")})
    return sorted(out, key=lambda c: c["taxon"].lower())


# --- assembling the file -------------------------------------------------------------

def assemble(
    records: list[dict[str, Any]],
    creatures: dict[str, dict[str, Any]],
    runs: list[dict[str, Any]],
    version_rows: list[dict[str, Any]],
    *,
    mode: str,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeline = versions.VersionTimeline(version_rows)
    for r in records:
        r["v"] = timeline.version_at(db.parse_iso(r["t"]))
    used = used_keys(records)
    return {
        "schema": SCHEMA,
        "generated_at": db.iso(now),
        "mode": mode,
        "app_version": config.APP_VERSION,
        "tiers": [[t, m] for t, m in config.TIERS],
        "tier_labels": config.TIER_LABELS,
        "ranges": [
            {"key": k, "label": config.RANK_FILTERS[k][2], "min": config.RANK_FILTERS[k][0],
             "max": config.RANK_FILTERS[k][1], "group": config.RANK_FILTERS[k][3]}
            for k in EXPORT_RANGES
        ],
        "versions": [{"version": v["version"], "starts_at": v["starts_at"], "basis": v["basis"]} for v in timeline.rows()],
        "sources": SOURCES,
        "stale_after_hours": config.STALE_AFTER_HOURS,
        "snapshots": records,
        "creatures": {k: v for k, v in creatures.items() if k in used},
        "credits": credits_list({k: v for k, v in creatures.items() if k in used}),
        "runs": runs,
        **(extra or {}),
    }


def build_local(conn: sqlite3.Connection, now: datetime, demo: bool = False) -> dict[str, Any]:
    records = snapshot_records(conn)
    creatures = creature_info(conn, used_keys(records))
    extra = {
        "demo": demo,
        "app_store_build": db.get_meta(conn, "app_store_build"),
        "unmatched": sorted(c["name"] for c in creatures.values() if not c["listed"]),
    }
    return assemble(records, creatures, run_entries(conn), db.game_versions(conn), mode="local", now=now, extra=extra)


# --- the public website ----------------------------------------------------------------

class Archive:
    """Snapshot history kept in the website's repository (archive/), so it survives
    even if the upstream feed ever removes old files. Append-only, one file per day."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.records: dict[str, dict[str, Any]] = {}
        self.creatures: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        snaps = self.folder / "snapshots"
        if snaps.is_dir():
            for path in sorted(snaps.glob("*.json")):
                for rec in json.loads(path.read_text(encoding="utf-8")):
                    self.records[rec["id"]] = rec
        self.creatures = self._read("creatures.json", {})
        self.runs = self._read("runs.json", [])
        self.versions = self._read("versions.json", [])
        self.meta = self._read("meta.json", {})

    def _read(self, name: str, default):
        path = self.folder / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write(self, name: str, data) -> None:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def ids_by_source(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for rec_id, rec in self.records.items():
            out[rec["src"]].add(rec_id.split(":", 1)[1])
        return out

    def add_records(self, records: Iterable[dict[str, Any]]) -> list[str]:
        added = []
        for rec in records:
            if rec["id"] not in self.records:
                rec = {k: v for k, v in rec.items() if k != "v"}
                self.records[rec["id"]] = rec
                added.append(rec["id"])
        return added

    def save(self) -> None:
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in self.records.values():
            by_day[rec["t"][:10]].append({k: v for k, v in rec.items() if k != "v"})
        snaps = self.folder / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        for day, recs in by_day.items():
            path = snaps / f"{day}.json"
            text = json.dumps(sorted(recs, key=lambda r: (r["t"], r["id"])), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        self._write("creatures.json", self.creatures)
        self._write("runs.json", self.runs[:RUN_HISTORY])
        self._write("versions.json", self.versions)
        self._write("meta.json", self.meta)


def build_website(out_dir: Path, archive_dir: Path, now_fn=db.utcnow, sources=None,
                  repo_url: str | None = None, collect_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update the archive from the feed and write a complete static website to out_dir."""
    archive = Archive(archive_dir)
    before = len(archive.records)
    conn = db.connect()
    try:
        now = now_fn()
        roster.ensure_roster(conn, now)
        with db.transaction(conn):
            versions.seed_known_versions(conn, now)
            for v in archive.versions:
                conn.execute(
                    "INSERT OR IGNORE INTO game_versions (version, starts_at, basis, first_build, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (v["version"], v["starts_at"], v["basis"], v.get("first_build"), db.iso(now)),
                )
        result = collector.run_collection(
            "scheduled", force=True, conn=conn, now_fn=now_fn, sources=sources,
            skip_ids=archive.ids_by_source(), **(collect_kwargs or {}),
        )
        added = archive.add_records(snapshot_records(conn))
        new_info = creature_info(conn, used_keys(archive.records.values()))
        for key, info in new_info.items():
            old_aka = set(archive.creatures.get(key, {}).get("aka", []))
            archive.creatures[key] = {**info, "aka": sorted(old_aka | set(info["aka"]))}
        latest_run = run_entries(conn, 1)
        archive.runs = latest_run + archive.runs
        archive.versions = [dict(v) for v in db.game_versions(conn)]
        build = db.get_meta(conn, "app_store_build")
        if build:
            archive.meta["app_store_build"] = build
        if len(archive.records) < before:
            raise RuntimeError("archive would shrink; refusing to publish")
        archive.save()

        records = sorted(archive.records.values(), key=lambda r: (r["t"], r["id"]))
        payload = assemble(
            records, archive.creatures, archive.runs, archive.versions, mode="website", now=now,
            extra={"repo_url": repo_url, "app_store_build": archive.meta.get("app_store_build"),
                   "unmatched": sorted(c["name"] for c in archive.creatures.values() if not c.get("listed", True))},
        )
        write_static_site(out_dir, payload)
        log.info("Website built: %d snapshots (%d new), update status: %s.", len(records), len(added), result.status)
        return {"status": result.status, "added": len(added), "total": len(records), "message": result.message}
    finally:
        conn.close()


def _remove_tree(path: Path) -> None:
    """Delete a folder, clearing read-only flags (OneDrive marks folders read-only on Windows)."""
    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def write_static_site(out_dir: Path, payload: dict[str, Any]) -> None:
    out_dir = Path(out_dir)
    if out_dir.exists():
        _remove_tree(out_dir)
    (out_dir / "data").mkdir(parents=True)
    for name in WEB_FILES:
        shutil.copyfile(config.WEB_DIR / name, out_dir / name)
    img_root = config.WEB_DIR / "img"
    for src in img_root.rglob("*"):
        if src.is_file() and src.suffix != ".tmp" and src.name not in ("credits.json", "standins.json"):
            dest = out_dir / "img" / src.relative_to(img_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)  # contents only: never copy read-only flags
    (out_dir / "data" / "site.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def repo_url_from_env() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}" if repo else None
