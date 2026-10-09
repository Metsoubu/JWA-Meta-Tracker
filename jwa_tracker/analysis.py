"""Reference implementation of the dashboard calculations (tier lists, comparisons, history).

The dashboard itself computes these in the browser from data/site.json; the
tests check that both give the same numbers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import config, db, standins, stats
from .sources import jwa_dashboard_feed
from .versions import VersionTimeline

SOURCE_INFO = {
    jwa_dashboard_feed.SOURCE_NAME: {
        "label": "jwa-dashboard public GitHub feed",
        "url": jwa_dashboard_feed.REPOSITORY_URL,
        "automatic": True,
    },
    "csv-import": {"label": "Manually imported CSV file", "url": None, "automatic": False},
    "example": {"label": "EXAMPLE DATA (randomly generated, not real)", "url": None, "automatic": False},
}


class SelectionError(ValueError):
    pass


class Analyzer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.timeline = VersionTimeline.load(conn)
        self.creatures = db.all_creatures(conn)
        self.images = standins.picture_index(self.creatures.keys())

    # --- helpers -------------------------------------------------------------------

    def version_of(self, snap: dict[str, Any]) -> str | None:
        return self.timeline.version_at(db.parse_iso(snap["captured_at"]))

    def snapshot_info(self, snap: dict[str, Any]) -> dict[str, Any]:
        source = SOURCE_INFO.get(snap["source"], {"label": snap["source"], "url": None, "automatic": False})
        return {
            "id": snap["id"],
            "captured_at": snap["captured_at"],
            "imported_at": snap["imported_at"],
            "kind": snap["kind"],
            "leaderboard_id": snap["leaderboard_id"],
            "leaderboard_name": snap["leaderboard_name"],
            "teams_valid": snap["teams_valid"],
            "teams_invalid": snap["teams_invalid"],
            "duplicates_dropped": snap["duplicates_dropped"],
            "rank_coverage_max": snap["rank_coverage_max"],
            "source": snap["source"],
            "source_label": source["label"],
            "source_url": source["url"],
            "game_version": self.version_of(snap),
            "is_example": bool(snap["is_example"]),
            "notes": snap["notes"],
        }

    def snapshots(self, kind: str) -> list[dict[str, Any]]:
        return db.list_snapshots(self.conn, kind)

    def teams_for(self, snapshot_ids: list[int]) -> list[stats.Team]:
        return stats.teams_from_rows(db.load_team_rows(self.conn, snapshot_ids))

    def teams_by_snapshot(self, snapshot_ids: list[int]) -> dict[int, list[stats.Team]]:
        grouped: dict[int, list[stats.Team]] = {sid: [] for sid in snapshot_ids}
        for team in self.teams_for(snapshot_ids):
            grouped[team.snapshot_id].append(team)
        return grouped

    def resolve_selection(self, kind: str, spec: str | None) -> dict[str, Any]:
        """spec: 'latest' | 'snap:<id>' | '<id>' | 'ver:<version>' -> selection description."""
        spec = (spec or "latest").strip()
        snaps = self.snapshots(kind)
        if not snaps:
            return {"mode": "none", "snapshots": [], "label": "No data yet", "spec": spec}
        if spec == "latest":
            return {"mode": "snapshot", "snapshots": [snaps[0]], "label": "Latest snapshot", "spec": f"snap:{snaps[0]['id']}"}
        if spec.startswith("ver:"):
            version = spec[4:]
            chosen = [s for s in snaps if self.version_of(s) == version]
            if not chosen:
                raise SelectionError(f"no {kind} snapshots for game version {version}")
            return {"mode": "version", "version": version, "snapshots": chosen,
                    "label": f"All {version} snapshots", "spec": spec}
        raw_id = spec[5:] if spec.startswith("snap:") else spec
        try:
            snap_id = int(raw_id)
        except ValueError as exc:
            raise SelectionError(f"invalid selection {spec!r}") from exc
        chosen = [s for s in snaps if s["id"] == snap_id]
        if not chosen:
            raise SelectionError(f"snapshot {snap_id} not found")
        return {"mode": "snapshot", "snapshots": chosen, "label": "Snapshot", "spec": f"snap:{snap_id}"}

    def _previous_spec(self, kind: str, selection: dict[str, Any]) -> str | None:
        snaps = self.snapshots(kind)  # newest first
        if selection["mode"] == "snapshot":
            current = selection["snapshots"][0]
            older = [s for s in snaps if (s["captured_at"], s["id"]) < (current["captured_at"], current["id"])]
            return f"snap:{older[0]['id']}" if older else None
        if selection["mode"] == "version":
            order = [v["version"] for v in self.timeline.rows()]
            present = {self.version_of(s) for s in snaps}
            try:
                idx = order.index(selection["version"])
            except ValueError:
                return None
            for v in reversed(order[:idx]):
                if v in present:
                    return f"ver:{v}"
        return None

    def _usage(self, selection: dict[str, Any], lo: int, hi: int) -> dict[str, Any]:
        snaps = selection["snapshots"]
        ids = [s["id"] for s in snaps]
        all_teams = self.teams_for(ids)
        teams, straddling = stats.filter_by_rank(all_teams, lo, hi)
        rows = stats.usage_table(teams, self.creatures)
        coverage_max = min(s["rank_coverage_max"] for s in snaps) if snaps else 0
        per_snapshot = round(len(teams) / len(snaps)) if snaps else 0
        return {
            "rows": rows,
            "teams": len(teams),
            "straddling": straddling,
            "coverage_max": coverage_max,
            "per_snapshot": per_snapshot,
            "invalid": db.invalid_team_count(self.conn, ids),
        }

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        img = self.images.get(row["key"])
        row["image"] = img["url"] if img else None
        row["image_kind"] = img["kind"] if img else None
        row["image_credit"] = img["credit"] if img else None
        return row

    # --- public API ----------------------------------------------------------------

    def tierlist(self, kind: str, range_key: str, spec: str | None) -> dict[str, Any]:
        lo, hi, label = stats.rank_filter(range_key)
        selection = self.resolve_selection(kind, spec)
        base = {
            "kind": kind,
            "range": {"key": range_key, "label": label, "min": lo, "max": hi},
            "tiers": [{"tier": t, "min": m, "label": config.TIER_LABELS[t]} for t, m in config.TIERS],
        }
        if selection["mode"] == "none":
            empty = stats.usage_table([], self.creatures)
            return {
                **base,
                "selection": {"mode": "none", "label": "No data yet", "snapshots": []},
                "sample": {"teams": 0, **stats.coverage_summary(lo, hi, 0, 0), "snapshot_count": 0},
                "creatures": [self._decorate({**r, "delta_pp": None, "prev_pct": None, "trend": []}) for r in empty],
                "previous": None,
            }
        usage = self._usage(selection, lo, hi)
        snaps = selection["snapshots"]
        coverage = stats.coverage_summary(lo, hi, usage["coverage_max"], usage["per_snapshot"])

        prev_spec = self._previous_spec(kind, selection)
        prev_map: dict[str, dict[str, Any]] = {}
        previous_info = None
        if prev_spec:
            prev_sel = self.resolve_selection(kind, prev_spec)
            prev_usage = self._usage(prev_sel, lo, hi)
            if prev_usage["teams"]:
                prev_map = {r["key"]: r for r in prev_usage["rows"]}
                previous_info = {
                    "spec": prev_spec,
                    "mode": prev_sel["mode"],
                    "version": prev_sel.get("version"),
                    "captured_at": prev_sel["snapshots"][0]["captured_at"] if prev_sel["mode"] == "snapshot" else None,
                    "teams": prev_usage["teams"],
                }

        trend_snaps = self._trend_window(kind, selection)
        per = self.teams_by_snapshot([s["id"] for s in trend_snaps])
        series = stats.trend_series(
            [(s["id"], stats.filter_by_rank(per[s["id"]], lo, hi)[0]) for s in trend_snaps],
            [r["key"] for r in usage["rows"]],
        )
        creatures = []
        for r in usage["rows"]:
            prev = prev_map.get(r["key"])
            r["prev_pct"] = prev["pct"] if prev else None
            r["delta_pp"] = (r["pct"] - prev["pct"]) if (prev and usage["teams"]) else None
            r["trend"] = series.get(r["key"], [])
            creatures.append(self._decorate(r))

        selection_out = {
            "mode": selection["mode"],
            "spec": selection["spec"],
            "label": selection["label"],
            "version": selection.get("version") or self.version_of(snaps[0]),
            "snapshot": self.snapshot_info(snaps[0]) if selection["mode"] == "snapshot" else None,
            "snapshot_count": len(snaps),
            "first_captured_at": min(s["captured_at"] for s in snaps),
            "last_captured_at": max(s["captured_at"] for s in snaps),
            "is_example": any(s["is_example"] for s in snaps),
            "sources": sorted({SOURCE_INFO.get(s["source"], {"label": s["source"]})["label"] for s in snaps}),
        }
        return {
            **base,
            "selection": selection_out,
            "sample": {
                "teams": usage["teams"],
                "snapshot_count": len(snaps),
                "excluded_invalid": usage["invalid"],
                "straddling": usage["straddling"],
                "coverage_max": usage["coverage_max"],
                **coverage,
            },
            "creatures": creatures,
            "previous": previous_info,
            "trend_points": [{"id": s["id"], "captured_at": s["captured_at"]} for s in trend_snaps],
        }

    def _trend_window(self, kind: str, selection: dict[str, Any]) -> list[dict[str, Any]]:
        snaps = sorted(selection["snapshots"], key=lambda s: (s["captured_at"], s["id"]))
        if selection["mode"] == "version":
            return snaps[-config.TREND_POINTS:]
        current = snaps[-1]
        older = [
            s for s in self.snapshots(kind)
            if (s["captured_at"], s["id"]) <= (current["captured_at"], current["id"])
        ]
        return sorted(older[: config.TREND_POINTS], key=lambda s: (s["captured_at"], s["id"]))

    def compare(self, kind: str, range_key: str, base_spec: str, current_spec: str) -> dict[str, Any]:
        lo, hi, label = stats.rank_filter(range_key)
        base_sel = self.resolve_selection(kind, base_spec)
        cur_sel = self.resolve_selection(kind, current_spec)
        if base_sel["mode"] == "none" or cur_sel["mode"] == "none":
            return {"rows": [], "base": None, "current": None, "range": {"key": range_key, "label": label}}
        base = self._usage(base_sel, lo, hi)
        cur = self._usage(cur_sel, lo, hi)
        rows = [self._decorate(r) for r in stats.compare(base["rows"], cur["rows"])]
        with_change = [r for r in rows if r["delta_pp"] is not None and (r["count"] or r["base_count"])]
        risers = sorted([r for r in with_change if r["delta_pp"] > 0], key=lambda r: -r["delta_pp"])[:10]
        fallers = sorted([r for r in with_change if r["delta_pp"] < 0], key=lambda r: r["delta_pp"])[:10]

        def side(sel, usage):
            snaps = sel["snapshots"]
            return {
                "spec": sel["spec"],
                "mode": sel["mode"],
                "version": sel.get("version") or self.version_of(snaps[0]),
                "captured_at": snaps[0]["captured_at"] if sel["mode"] == "snapshot" else None,
                "first_captured_at": min(s["captured_at"] for s in snaps),
                "last_captured_at": max(s["captured_at"] for s in snaps),
                "snapshot_count": len(snaps),
                "teams": usage["teams"],
                "coverage_max": usage["coverage_max"],
                "is_example": any(s["is_example"] for s in snaps),
            }

        return {
            "range": {"key": range_key, "label": label, "min": lo, "max": hi},
            "base": side(base_sel, base),
            "current": side(cur_sel, cur),
            "rows": rows,
            "risers": risers,
            "fallers": fallers,
            "tier_changes": [r for r in rows if r["tier"] != r["base_tier"] and r["total"] and r["base_total"]],
        }

    def history(self, kind: str, range_key: str, keys: list[str] | None, limit: int = 120) -> dict[str, Any]:
        lo, hi, label = stats.rank_filter(range_key)
        snaps = sorted(self.snapshots(kind)[:limit], key=lambda s: (s["captured_at"], s["id"]))
        per = self.teams_by_snapshot([s["id"] for s in snaps])
        filtered = [(s["id"], stats.filter_by_rank(per[s["id"]], lo, hi)[0]) for s in snaps]
        if not keys:
            latest = filtered[-1][1] if filtered else []
            counts, _ = stats.count_usage(latest)
            keys = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:6]]
        series = stats.trend_series(filtered, keys)
        return {
            "range": {"key": range_key, "label": label},
            "points": [
                {"id": s["id"], "captured_at": s["captured_at"], "teams": len(teams),
                 "version": self.version_of(s), "leaderboard_name": s["leaderboard_name"]}
                for s, (_sid, teams) in zip(snaps, filtered)
            ],
            "series": [
                self._decorate({
                    "key": k,
                    "name": self.creatures.get(k, {}).get("name", k),
                    "rarity": self.creatures.get(k, {}).get("rarity"),
                    "values": series[k],
                })
                for k in keys
            ],
        }

    def creature_detail(self, kind: str, key: str, spec: str | None, range_key: str) -> dict[str, Any]:
        info = self.creatures.get(key)
        if not info:
            raise SelectionError(f"unknown creature {key!r}")
        selection = self.resolve_selection(kind, spec)
        brackets = []
        if selection["mode"] != "none":
            teams_all = self.teams_for([s["id"] for s in selection["snapshots"]])
            coverage = min(s["rank_coverage_max"] for s in selection["snapshots"])
            for rk, (lo, hi, label, group) in config.RANK_FILTERS.items():
                teams, _ = stats.filter_by_rank(teams_all, lo, hi)
                counts, total = stats.count_usage(teams)
                brackets.append({
                    "key": rk, "label": label, "group": group, "count": counts.get(key, 0), "total": total,
                    "pct": stats.percentage(counts.get(key, 0), total),
                    "tier": stats.tier_for(counts.get(key, 0), total) if total else None,
                    "coverage": stats.coverage_summary(lo, hi, coverage, total)["state"],
                })
        names = [
            dict(r) for r in self.conn.execute(
                "SELECT source, display_name, match_method FROM source_creatures WHERE creature_key = ? "
                "ORDER BY display_name", (key,)
            )
        ]
        hist = self.history(kind, range_key, [key], limit=120)
        return {
            "creature": self._decorate({
                "key": key, "name": info["name"], "rarity": info["rarity"], "class": info["class"],
                "hybrid_type": info["hybrid_type"], "added_version": info["added_version"],
                "in_roster": bool(info["in_roster"]), "roster_source": info["roster_source"],
            }),
            "brackets": brackets,
            "source_names": names,
            "history": {"points": hist["points"], "values": hist["series"][0]["values"] if hist["series"] else []},
            "range": hist["range"],
        }

    def snapshot_list(self, kind: str) -> dict[str, Any]:
        snaps = self.snapshots(kind)
        versions: dict[str, int] = {}
        out = []
        for s in snaps:
            info = self.snapshot_info(s)
            out.append(info)
            if info["game_version"]:
                versions[info["game_version"]] = versions.get(info["game_version"], 0) + 1
        ordered = [v["version"] for v in reversed(self.timeline.rows()) if v["version"] in versions]
        return {
            "kind": kind,
            "snapshots": out,
            "versions": [{"version": v, "snapshot_count": versions[v]} for v in ordered],
            "unversioned": sum(1 for s in out if not s["game_version"]),
        }

    def creature_search_index(self) -> list[dict[str, Any]]:
        return [
            self._decorate({"key": k, "name": c["name"], "rarity": c["rarity"]})
            for k, c in sorted(self.creatures.items(), key=lambda kv: kv[1]["name"].lower())
            if c["in_roster"]
        ]
