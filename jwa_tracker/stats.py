"""Usage statistics and popularity tiers (pure functions, no database access).

usage % = teams containing the creature / valid teams in the sample x 100

Tiers compare exact counts with integer arithmetic (count * 100 >= threshold *
total), so 79.99% can never be rounded up into S tier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import config


@dataclass(frozen=True)
class Team:
    id: int
    snapshot_id: int
    rank_min: int
    rank_max: int
    creatures: frozenset[str]


def teams_from_rows(rows: Iterable[Any]) -> list[Team]:
    """Group (team_id, snapshot_id, rank_min, rank_max, creature_key) rows into teams."""
    grouped: dict[int, dict[str, Any]] = {}
    for r in rows:
        entry = grouped.setdefault(
            r["team_id"],
            {"snapshot_id": r["snapshot_id"], "rank_min": r["rank_min"], "rank_max": r["rank_max"], "c": set()},
        )
        entry["c"].add(r["creature_key"])
    return [
        Team(tid, e["snapshot_id"], e["rank_min"], e["rank_max"], frozenset(e["c"]))
        for tid, e in grouped.items()
    ]


def tier_for(count: int, total: int) -> str:
    if total <= 0:
        return "D"
    for letter, threshold in config.TIERS:
        if count * 100 >= threshold * total:
            return letter
    return "D"


def percentage(count: int, total: int) -> float:
    return (count * 100.0 / total) if total else 0.0


def rank_filter(key: str) -> tuple[int, int, str]:
    if key not in config.RANK_FILTERS:
        raise KeyError(f"unknown rank filter {key!r}")
    lo, hi, label, _group = config.RANK_FILTERS[key]
    return lo, hi, label


def filter_by_rank(teams: Iterable[Team], lo: int, hi: int) -> tuple[list[Team], int]:
    """Teams whose whole rank band lies inside [lo, hi], plus the count that straddle it.

    Bands that only partly overlap the range cannot be placed precisely, so they
    are left out and reported rather than guessed.
    """
    inside: list[Team] = []
    straddling = 0
    for t in teams:
        if t.rank_min >= lo and t.rank_max <= hi:
            inside.append(t)
        elif t.rank_min <= hi and t.rank_max >= lo:
            straddling += 1
    return inside, straddling


def count_usage(teams: Iterable[Team]) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    total = 0
    for t in teams:
        total += 1
        for key in t.creatures:
            counts[key] = counts.get(key, 0) + 1
    return counts, total


def usage_table(
    teams: list[Team],
    creatures: dict[str, dict[str, Any]],
    include_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Every creature with its count, exact percentage and tier, highest usage first.

    `creatures` maps key -> creature info. Creatures that are in the roster are
    always listed, even at 0%. Unmatched source creatures are listed when used.
    """
    counts, total = count_usage(teams)
    keys = {k for k, c in creatures.items() if c.get("in_roster", 1)}
    keys.update(counts)
    if include_keys:
        keys.update(include_keys)
    rows = []
    for key in keys:
        info = creatures.get(key, {"key": key, "name": key})
        count = counts.get(key, 0)
        rows.append(
            {
                "key": key,
                "name": info.get("name", key),
                "rarity": info.get("rarity"),
                "class": info.get("class"),
                "hybrid_type": info.get("hybrid_type"),
                "in_roster": bool(info.get("in_roster", 1)),
                "count": count,
                "total": total,
                "pct": percentage(count, total),
                "tier": tier_for(count, total),
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["name"].lower()))
    return rows


def coverage_summary(lo: int, hi: int, coverage_max: int, teams_in_range: int) -> dict[str, Any]:
    """Describe how much of the requested rank range the data actually covers."""
    expected = hi - lo + 1
    if coverage_max <= 0 or lo > coverage_max:
        state = "none"
        message = (
            f"The current data source only covers ranks 1–{coverage_max}, so there are no teams "
            f"ranked {lo}–{hi} to count."
            if coverage_max > 0
            else "No data has been collected yet."
        )
    elif hi > coverage_max:
        state = "partial"
        message = (
            f"The data source only covers ranks 1–{coverage_max}, so this view uses the "
            f"{teams_in_range} teams ranked {lo}–{coverage_max} instead of {expected}."
        )
    else:
        state = "complete"
        message = ""
    return {
        "state": state,
        "message": message,
        "ranks_requested": [lo, hi],
        "ranks_covered": [lo, min(hi, coverage_max)] if state != "none" else None,
        "teams_expected_per_snapshot": expected,
    }


def trend_series(
    per_snapshot_teams: list[tuple[int, list[Team]]], keys: Iterable[str]
) -> dict[str, list[float | None]]:
    """Usage % per snapshot (oldest first) for each key. None where a snapshot has no sample."""
    keys = list(keys)
    series: dict[str, list[float | None]] = {k: [] for k in keys}
    for _snapshot_id, teams in per_snapshot_teams:
        counts, total = count_usage(teams)
        for k in keys:
            series[k].append(percentage(counts.get(k, 0), total) if total else None)
    return series


def compare(
    base_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Join two usage tables and compute the change in percentage points."""
    base = {r["key"]: r for r in base_rows}
    out = []
    for r in current_rows:
        b = base.get(r["key"])
        b_pct = b["pct"] if b else 0.0
        b_has_sample = bool(b and b["total"])
        out.append(
            {
                "key": r["key"],
                "name": r["name"],
                "rarity": r["rarity"],
                "class": r.get("class"),
                "in_roster": r["in_roster"],
                "base_count": b["count"] if b else 0,
                "base_total": b["total"] if b else 0,
                "base_pct": b_pct,
                "base_tier": b["tier"] if b else "D",
                "count": r["count"],
                "total": r["total"],
                "pct": r["pct"],
                "tier": r["tier"],
                "delta_pp": (r["pct"] - b_pct) if (b_has_sample and r["total"]) else None,
            }
        )
    seen = {r["key"] for r in current_rows}
    for key, b in base.items():
        if key not in seen:
            out.append(
                {
                    "key": key, "name": b["name"], "rarity": b["rarity"], "class": b.get("class"),
                    "in_roster": b["in_roster"], "base_count": b["count"], "base_total": b["total"],
                    "base_pct": b["pct"], "base_tier": b["tier"], "count": 0, "total": 0, "pct": 0.0,
                    "tier": "D", "delta_pp": None,
                }
            )
    return out
