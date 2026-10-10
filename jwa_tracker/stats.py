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


# --- creature details (reference for the dashboard's creature panel) --------------------
# A detailed team is (rank_min, rank_max, members); a member is
# (creature_key, level, enhancement, boosts, omega) where boosts/omega are
# {stat: points} dicts or None. The browser (web/app.js `creatureProfile`)
# computes exactly the same from data/details/*.json; the tests check both.

BOOST_ORDER = ("Attack", "Health", "Speed")
OMEGA_ORDER = ("Health", "Attack", "Speed", "Armor", "Crit", "Crit Dmg")


def ordered_stats(names: Iterable[str], preferred: tuple[str, ...]) -> list[str]:
    names = set(names)
    return [n for n in preferred if n in names] + sorted(names - set(preferred))


def rank_groups(lo: int, hi: int) -> list[tuple[int, int]]:
    """At most ten equal rank groups covering lo..hi (10, 25 or 50 ranks each)."""
    span = hi - lo + 1
    width = next((w for w in (10, 25, 50) if span <= 10 * w), 50)
    return [(a, min(hi, a + width - 1)) for a in range(lo, hi + 1, width)]


def _ranked(counter: dict[Any, int]) -> list[tuple[Any, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def creature_profile(teams: Iterable[tuple], key: str, lo: int, hi: int, top_splits: int = 3) -> dict[str, Any]:
    """Who a creature is paired with, where in lo..hi it is used, and how it is built."""
    groups = rank_groups(lo, hi)
    width = groups[0][1] - groups[0][0] + 1
    group_teams = [0] * len(groups)
    group_used = [0] * len(groups)
    total = used = 0
    mates: dict[str, int] = {}
    builds = []
    for rank_min, rank_max, members in teams:
        if rank_min < lo or rank_max > hi:
            continue  # same rule as filter_by_rank: only teams wholly inside the range
        g = (rank_min - lo) // width
        total += 1
        group_teams[g] += 1
        mine = next((m for m in members if m[0] == key), None)
        if mine is None:
            continue
        used += 1
        group_used[g] += 1
        for m in members:
            if m[0] != key:
                mates[m[0]] = mates.get(m[0], 0) + 1
        if any(v is not None for v in mine[1:]):
            builds.append(mine)

    levels: dict[float, int] = {}
    enhancements: dict[int, int] = {}
    splits: dict[tuple, int] = {}
    full: dict[tuple, int] = {}  # (level, enhancement, *boost split) for builds that publish all three
    complete = 0
    boost_sums: dict[str, float] = {}
    omega_sums: dict[str, float] = {}
    boosted = omegas = 0
    boost_stats = ordered_stats({s for b in builds if b[3] for s in b[3]}, BOOST_ORDER)
    omega_stats = ordered_stats({s for b in builds if b[4] for s in b[4]}, OMEGA_ORDER)
    for _key, level, enhancement, boosts, omega in builds:
        if level is not None:
            levels[level] = levels.get(level, 0) + 1
        if enhancement is not None:
            enhancements[enhancement] = enhancements.get(enhancement, 0) + 1
        if boosts is not None:
            boosted += 1
            split = tuple(boosts.get(s, 0) for s in boost_stats)  # a stat left out means no points
            splits[split] = splits.get(split, 0) + 1
            for s in boost_stats:
                boost_sums[s] = boost_sums.get(s, 0) + boosts.get(s, 0)
            if level is not None and enhancement is not None:
                complete += 1
                whole = (level, enhancement, *split)
                full[whole] = full.get(whole, 0) + 1
        if omega:
            omegas += 1
            for s in omega_stats:
                omega_sums[s] = omega_sums.get(s, 0) + omega.get(s, 0)
    return {
        "teams": total,
        "used": used,
        "groups": [{"lo": a, "hi": b, "teams": group_teams[i], "used": group_used[i]} for i, (a, b) in enumerate(groups)],
        "teammates": _ranked(mates),
        "builds": {
            "count": len(builds),
            "levels": _ranked(levels),
            "enhancements": _ranked(enhancements),
            "boost_stats": boost_stats,
            "complete": complete,
            "top_builds": _ranked(full)[:top_splits],
            "boosted": boosted,
            "splits": _ranked(splits)[:top_splits],
            "boost_avg": {s: boost_sums[s] / boosted for s in boost_stats} if boosted else {},
            "omega_stats": omega_stats,
            "omegas": omegas,
            "omega_avg": {s: omega_sums[s] / omegas for s in omega_stats} if omegas else {},
        },
    }


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
