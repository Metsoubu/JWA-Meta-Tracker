"""Everything the dashboard shows, as one JSON file (data/site.json).

The dashboard (web/app.js) is a static page: it loads data/site.json and does
the pooling, tiers and comparisons in the browser. The same files therefore work
as your local dashboard (the local server builds site.json from your database)
and as a free public website on GitHub Pages, where a scheduled GitHub Action
runs `tracker.py build-site` every 3 hours.

For each snapshot the file holds, per rank range, the number of valid teams and
how many of them use each creature. Counting is done here, in tested Python; the
browser only adds counts together and divides.

The creature panel (best teammates, where in the ranking a creature is used, how
top players build it) needs the teams themselves. Those are published separately,
one file per month (data/details/YYYY-MM.json), and only loaded when somebody
opens a creature. The browser's counting there mirrors stats.creature_profile.

Nothing personal is included: no file paths, user names or computer settings.
The feed itself is anonymous (rank bands of ten, no player names).
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
DETAILS_SCHEMA = "jwa-meta-tracker.details.v1"
_MONTH = re.compile(r"^\d{4}-\d{2}$")
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


# --- team details (creature panel) --------------------------------------------------
# In memory: {record id: [(rank_min, rank_max, [(key, level, enhancement, boosts, omega), ...]), ...]}
# with boosts/omega as {stat: points} or None (see stats.creature_profile).

def _num(value: Any) -> int | float | None:
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


def _points(text: str | None) -> dict[str, Any] | None:
    return None if text is None else {k: _num(v) for k, v in json.loads(text).items()}


def team_details(conn: sqlite3.Connection, snaps: list[dict[str, Any]] | None = None) -> dict[str, list[tuple]]:
    """Every valid team of the given snapshots (default: all) with each member's build, if known."""
    snaps = db.list_snapshots(conn) if snaps is None else snaps
    record_id = {s["id"]: f"{s['source']}:{s['source_snapshot_id']}" for s in snaps}
    out: dict[str, list[tuple]] = {}
    team_id = None
    for r in db.load_team_members(conn, list(record_id)):
        teams = out.setdefault(record_id[r["snapshot_id"]], [])
        if r["team_id"] != team_id:
            team_id = r["team_id"]
            teams.append((r["rank_min"], r["rank_max"], []))
        teams[-1][2].append((r["creature_key"], _num(r["level"]), _num(r["enhancement"]),
                             _points(r["boosts"]), _points(r["omega"])))
    return out


def encode_details(details: dict[str, list[tuple]]) -> dict[str, Any]:
    """Compact JSON form: creature keys and stat names are listed once and referred to by position.

    A team is [rank_min, rank_max, member x 8]; a member is [key#] without a build, else
    [key#, level, enhancement, boosts (one number per boost stat, or null), omega (optional)].
    A stat missing from a published build counts as 0 points.
    """
    all_members = [m for teams in details.values() for t in teams for m in t[2]]
    keys = sorted({m[0] for m in all_members})
    boost_stats = stats.ordered_stats({s for m in all_members if m[3] for s in m[3]}, stats.BOOST_ORDER)
    omega_stats = stats.ordered_stats({s for m in all_members if m[4] for s in m[4]}, stats.OMEGA_ORDER)
    index = {k: i for i, k in enumerate(keys)}

    def member(m):
        key, level, enhancement, boosts, omega = m
        if level is None and enhancement is None and boosts is None and not omega:
            return [index[key]]
        out = [index[key], level, enhancement, None if boosts is None else [boosts.get(s, 0) for s in boost_stats]]
        if omega:
            out.append([omega.get(s, 0) for s in omega_stats])
        return out

    return {
        "schema": DETAILS_SCHEMA,
        "keys": keys,
        "boost_stats": boost_stats,
        "omega_stats": omega_stats,
        "snaps": {rid: [[lo, hi, *map(member, members)] for lo, hi, members in teams]
                  for rid, teams in sorted(details.items())},
    }


def decode_details(doc: dict[str, Any]) -> dict[str, list[tuple]]:
    keys, boost_stats, omega_stats = doc["keys"], doc["boost_stats"], doc["omega_stats"]

    def member(m):
        if len(m) == 1:
            return (keys[m[0]], None, None, None, None)
        boosts = None if m[3] is None else dict(zip(boost_stats, m[3]))
        omega = dict(zip(omega_stats, m[4])) if len(m) > 4 else None
        return (keys[m[0]], m[1], m[2], boosts, omega)

    return {rid: [(t[0], t[1], [member(m) for m in t[2:]]) for t in teams] for rid, teams in doc["snaps"].items()}


def details_by_month(records: Iterable[dict[str, Any]], details: dict[str, list[tuple]]) -> dict[str, dict[str, Any]]:
    """Encoded detail files keyed by month ("2026-10"), from each snapshot's capture time (UTC)."""
    month_of = {r["id"]: r["t"][:7] for r in records}
    grouped: dict[str, dict[str, list[tuple]]] = defaultdict(dict)
    for rid, teams in details.items():
        if rid in month_of:
            grouped[month_of[rid]][rid] = teams
    return {month: encode_details(d) for month, d in sorted(grouped.items())}


def details_month_local(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    if not _MONTH.match(month):
        raise ValueError(f"not a month: {month!r}")
    snaps = [s for s in db.list_snapshots(conn) if s["captured_at"][:7] == month]
    return encode_details(team_details(conn, snaps))


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
    detail_ids: set[str] | None = None,
) -> dict[str, Any]:
    timeline = versions.VersionTimeline(version_rows)
    for r in records:
        r["v"] = timeline.version_at(db.parse_iso(r["t"]))
        r.pop("d", None)
        if detail_ids and r["id"] in detail_ids:
            r["d"] = 1  # its teams are in data/details/<month>.json
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
    return assemble(records, creatures, run_entries(conn), db.game_versions(conn), mode="local", now=now, extra=extra,
                    detail_ids={r["id"] for r in records})


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
        self.details: dict[str, list[tuple]] = {}  # record id -> teams with builds (see team_details)
        snaps = self.folder / "snapshots"
        if snaps.is_dir():
            for path in sorted(snaps.glob("*.json")):
                for rec in json.loads(path.read_text(encoding="utf-8")):
                    self.records[rec["id"]] = rec
        teams = self.folder / "teams"
        if teams.is_dir():
            for path in sorted(teams.glob("*.json")):
                self.details.update(decode_details(json.loads(path.read_text(encoding="utf-8"))))
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
                rec = {k: v for k, v in rec.items() if k not in ("v", "d")}
                self.records[rec["id"]] = rec
                added.append(rec["id"])
        return added

    def missing_details(self) -> set[str]:
        return {rid for rid in self.records if rid not in self.details}

    def add_details(self, details: dict[str, list[tuple]]) -> list[str]:
        """Teams of archived snapshots that have none yet (existing entries are never replaced)."""
        added = [rid for rid in details if rid in self.records and rid not in self.details]
        for rid in added:
            self.details[rid] = details[rid]
        return added

    @staticmethod
    def _write_if_changed(path: Path, text: str) -> None:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")

    def save(self) -> None:
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in self.records.values():
            by_day[rec["t"][:10]].append({k: v for k, v in rec.items() if k not in ("v", "d")})
        snaps = self.folder / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        for day, recs in by_day.items():
            text = json.dumps(sorted(recs, key=lambda r: (r["t"], r["id"])), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
            self._write_if_changed(snaps / f"{day}.json", text)
        details_by_day: dict[str, dict[str, list[tuple]]] = defaultdict(dict)
        for rid, teams in self.details.items():
            if rid in self.records:
                details_by_day[self.records[rid]["t"][:10]][rid] = teams
        if details_by_day:
            folder = self.folder / "teams"
            folder.mkdir(parents=True, exist_ok=True)
            for day, details in details_by_day.items():
                text = json.dumps(encode_details(details), ensure_ascii=False, separators=(",", ":"))
                self._write_if_changed(folder / f"{day}.json", text)
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
        # Archived snapshots saved before team details were kept are downloaded once more
        # (if the feed still has them) so their teams and builds can be added.
        missing = archive.missing_details()
        skip = {src: {sid for sid in ids if f"{src}:{sid}" not in missing}
                for src, ids in archive.ids_by_source().items()}
        result = collector.run_collection(
            "scheduled", force=True, conn=conn, now_fn=now_fn, sources=sources,
            skip_ids=skip, **(collect_kwargs or {}),
        )
        added = archive.add_records(snapshot_records(conn))
        archive.add_details(team_details(conn))
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
            detail_ids=set(archive.details),
        )
        write_static_site(out_dir, payload, details_by_month(records, archive.details))
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


def write_static_site(out_dir: Path, payload: dict[str, Any], details: dict[str, dict[str, Any]] | None = None) -> None:
    """The complete website. `details` maps a month ("2026-10") to its encoded team-details file."""
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
    if details:
        (out_dir / "data" / "details").mkdir()
        for month, doc in details.items():
            if not _MONTH.match(month):
                raise ValueError(f"not a month: {month!r}")
            (out_dir / "data" / "details" / f"{month}.json").write_text(
                json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def repo_url_from_env() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}" if repo else None
