"""Work out which creature a nameless feed code is, from the feed's own evidence.

The feed's name list lags behind the game: creatures newer than it arrive with only
a code (e.g. "2b88a0f8") and no name or picture. For every such code this module
narrows the creature list down:

- rarity: the feed's own rarity if it gives one; otherwise the tournament's allowed
  rarities, the lowest level seen (no creature is below its rarity's starting level)
  and omega training points (only omega creatures have them);
- newness: a nameless creature is newer than the feed's official name list, so only
  creatures from the newest game version that list covers, or later, qualify; creatures
  the feed already accounts for are ruled out. Newer creatures reach the feed one at a
  time under internal names ("Guidraco", "Alioramu"), in no particular order, so those
  say nothing about the ones still without a name;
- alphabetical place: tournaments list their eligible creatures alphabetically by
  English name, so where the code sits between two named creatures, its name must
  fall between theirs;
- no sharing: two codes cannot be the same creature. If more codes fit than there are
  creatures for them, at least one is a creature the list does not have yet, and none
  of them is named.

If exactly one creature is left it is used, and its page says it was worked out
automatically and why. A brand-new creature can reach the feed before the creature
list (Paleo.gg) has it, so nothing is named until the list has been refreshed at
least GRACE_DAYS after the code first appeared; until then the page shows the
possibilities. If a few are left, the page lists them as possibilities and the
creature stays "Unidentified". Nothing is guessed beyond that.

Every code is re-checked on every update. What each snapshot showed is kept with the
conclusion (the `evidence` column), because the public website starts each build
with an empty database and only sees that run's new snapshots; its archive carries
the evidence and the conclusions from one build to the next (`export` / `load`), and
`archive_creatures` tells it which creatures the feed has named over all of history.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from . import db, roster

START_LEVEL = {"common": 1, "rare": 6, "epic": 11, "legendary": 16, "unique": 21, "apex": 26, "omega": 1}
NAMED_METHODS = ("exact", "localized", "translated")  # the feed knew the creature's real name
MAX_POSSIBILITIES = 4
GRACE_DAYS = 3
# The newest game version the feed's official name list (the names in capitals) covers, checked by hand
# against its tournament lists. If the feed extends the list, this being out of date only means
# a few more possibilities for a nameless code, never a wrong name.
OFFICIAL_LIST_VERSION = "v3.20"

is_code = roster.is_code_label


def version_key(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").lstrip("v").split("."))
    except ValueError:
        return (0,)


def _join(words: list[str], word: str = "and") -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + f" {word} " + words[-1]


def _neighbours(eligible: list[str], label: str) -> list[str | None] | None:
    """The nearest named creatures before and after a code in a tournament's eligible list."""
    if label not in eligible:
        return None
    i = eligible.index(label)
    return [next((n for n in reversed(eligible[:i]) if not is_code(n)), None),
            next((n for n in eligible[i + 1:] if not is_code(n)), None)]


def _snapshot_evidence(conn: sqlite3.Connection, source: str, source_id: str, label: str) -> dict[str, dict[str, Any]]:
    """What each stored snapshot shows about a code: {snapshot id: {...}}."""
    rules = db.build_rules(conn)
    out: dict[str, dict[str, Any]] = {}
    for o in conn.execute(
        """
        SELECT t.snapshot_id, s.source_snapshot_id, s.leaderboard_name, s.captured_at, b.level, b.omega
        FROM team_members m
        JOIN teams t ON t.id = m.team_id
        JOIN snapshots s ON s.id = t.snapshot_id
        LEFT JOIN member_builds b ON b.team_id = m.team_id AND b.slot = m.slot
        WHERE s.source = ? AND m.source_creature_id = ?
        """,
        (source, source_id),
    ):
        e = out.get(o["source_snapshot_id"])
        if e is None:
            rule = rules.get(o["snapshot_id"], {})
            e = out[o["source_snapshot_id"]] = {
                "leaderboard": o["leaderboard_name"], "captured_at": o["captured_at"], "copies": 0, "lowest": None,
                "omega": False, "allowed": rule.get("rarities") or [],
                "neighbours": _neighbours(rule.get("eligible") or [], label),
            }
        if o["level"] is not None:
            e["copies"] += 1
            e["lowest"] = o["level"] if e["lowest"] is None else min(e["lowest"], o["level"])
        if o["omega"] and json.loads(o["omega"]):
            e["omega"] = True
    return out


def _known_creatures(conn: sqlite3.Connection, resolver: roster.Resolver, source: str,
                     archive_creatures: dict[str, dict[str, Any]] | None) -> dict[str, set[str]]:
    """The labels each creature is known by in the feed."""
    labels: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT creature_key, display_name, match_method FROM source_creatures WHERE source = ?", (source,)):
        if r["match_method"] != "unmatched":
            labels[r["creature_key"]].add((r["display_name"] or "").strip().lower())
    for key, info in (archive_creatures or {}).items():
        if key in resolver.roster:
            labels[key].update(name.strip().lower() for name in info.get("aka") or ["(seen on a team)"])
    return labels


def judge(resolver: roster.Resolver, labels: dict[str, set[str]], cutoff: str | None, label: str,
          rarity_label: str | None, evidence: dict[str, dict[str, Any]], listed: datetime | None) -> dict[str, Any]:
    """{"key": creature key or None, "possible": [keys], "fits": how many creatures fit, "note": explanation}
    for one nameless code."""
    cast = resolver.roster
    snapshots = sorted(evidence.values(), key=lambda e: e["captured_at"])
    first_seen = snapshots[0]["captured_at"] if snapshots else None

    reasons: list[str] = []
    rarity = (rarity_label or "").lower()
    if rarity in START_LEVEL:
        possible = {rarity}
        reasons.append(f"the data source lists it as {rarity.title()}")
    else:
        possible = set(START_LEVEL)
        for leaderboard, allowed in sorted({(e["leaderboard"], tuple(e.get("allowed") or ())) for e in snapshots}):
            if allowed:
                possible &= {a.lower() for a in allowed}
                reasons.append(f"{leaderboard} allowed only {_join(list(allowed))} creatures")
        levels = [e["lowest"] for e in snapshots if e.get("lowest") is not None]
        if levels:
            possible = {r for r in possible if START_LEVEL[r] <= min(levels)}
            reasons.append(f"its lowest level is {min(levels):g}")
        if any(e.get("omega") for e in snapshots):
            possible &= {"omega"}
            reasons.append("it has omega training")
        elif levels:
            possible.discard("omega")
            copies = sum(e.get("copies") or 0 for e in snapshots)
            reasons.append(f"none of its {copies} team appearances has omega training")

    own = label.strip().lower()
    candidates = [c for c in cast.values()
                  if c.get("rarity") in possible and not labels.get(c["key"], set()) - {own}
                  and (cutoff is None or version_key(c.get("added_version")) >= version_key(cutoff))]

    slot = None
    for e in snapshots:
        before, after = e.get("neighbours") or (None, None)
        names = []
        for neighbour in (before, after):
            match = resolver.resolve("", "", neighbour, None) if neighbour else None
            names.append(cast[match.key]["name"] if match and match.key else None)
        if names[0] and names[1] and roster.normalize(names[0]) < roster.normalize(names[1]):
            candidates = [c for c in candidates
                          if roster.normalize(names[0]) < roster.normalize(c["name"]) < roster.normalize(names[1])]
            slot = (e["leaderboard"], names[0], names[1])
            break
    candidates.sort(key=lambda c: (version_key(c.get("added_version")), c["name"]))

    intro = _intro(label, first_seen)
    if cutoff:
        reasons.append(f"the data source's official name list goes up to game version {cutoff.lstrip('v')}, "
                       "so a creature without any name is from that version or later")
    if slot:
        reasons.append(f"in {slot[0]}'s alphabetical list of eligible creatures it sits between {slot[1]} and {slot[2]}")
    why = ("; ".join(reasons) + ".") if reasons else ""

    waiting = first_seen is not None and (listed is None or listed < db.parse_iso(first_seen) + timedelta(days=GRACE_DAYS))
    if waiting and candidates:
        names = _join([c["name"] for c in candidates[:MAX_POSSIBILITIES]], "or")
        more = " (or another creature)" if len(candidates) > MAX_POSSIBILITIES else ""
        return {"key": None, "possible": [c["key"] for c in candidates[:MAX_POSSIBILITIES]], "fits": len(candidates),
                "note": (f"{intro} So far it fits {names}{more}. It is named only once the creature list has had "
                         f"{GRACE_DAYS} days to add brand-new creatures, so it is not mistaken for an older one.")}
    if len(candidates) == 1:
        c = candidates[0]
        return {"key": c["key"], "possible": [c["key"]], "fits": 1,
                "note": (f"{intro} Worked out automatically as {c['name']} ({(c.get('rarity') or '').title()}, game "
                         f"version {(c.get('added_version') or '?').lstrip('v')}): {why[:1].lower() + why[1:]} "
                         f"{c['name']} is the only creature that fits and is not already named by the data source.")}
    if 1 < len(candidates) <= MAX_POSSIBILITIES:
        return {"key": None, "possible": [c["key"] for c in candidates], "fits": len(candidates),
                "note": (f"{intro} It is probably {_join([c['name'] for c in candidates], 'or')}, but the evidence "
                         f"so far cannot tell which: {why[:1].lower() + why[1:]}")}
    return {"key": None, "possible": [], "fits": len(candidates),
            "note": f"{intro} There is not enough evidence yet to tell which creature it is."}


def _intro(label: str, first_seen: str | None) -> str:
    return f"The data source gives no name for this creature (code {label}" + (
        f", first seen {first_seen[:10]})." if first_seen else ").")


def _judge_together(resolver: roster.Resolver, labels: dict[str, set[str]], cutoff: str | None,
                    codes: dict[str, dict[str, Any]], listed: datetime | None) -> dict[str, dict[str, Any]]:
    """Judge every open code at once, because two codes cannot be the same creature.

    A code that alone fits a creature takes it, which can settle the others. Codes competing for
    too few creatures all stay unnamed: however they were dealt out, at least one would be wrong.
    """
    labels = {key: set(names) for key, names in labels.items()}
    while True:
        findings = {i: judge(resolver, labels, cutoff, c["label"], c["rarity"], c["evidence"], listed)
                    for i, c in codes.items()}
        claims: dict[str, list[str]] = defaultdict(list)
        for i, finding in findings.items():
            if finding["key"]:
                claims[finding["key"]].append(i)
        taken = {key: who[0] for key, who in claims.items()
                 if len(who) == 1 and codes[who[0]]["label"].lower() not in labels.get(key, ())}
        if not taken:
            break
        for key, i in taken.items():
            labels.setdefault(key, set()).add(codes[i]["label"].lower())

    rivals: dict[frozenset[str], list[str]] = defaultdict(list)
    for i, finding in findings.items():
        if finding["possible"] and finding["fits"] == len(finding["possible"]):  # the full list, not a shortened one
            rivals[frozenset(finding["possible"])].append(i)
    for keys, who in rivals.items():
        if len(who) <= len(keys):
            continue
        names = _join(sorted(resolver.roster[k]["name"] for k in keys), "or")
        for i in who:
            shown = sorted(e["captured_at"] for e in codes[i]["evidence"].values())
            findings[i] = {"key": None, "possible": sorted(keys), "fits": len(keys),
                           "note": (f"{_intro(codes[i]['label'], shown[0] if shown else None)} {len(who)} different "
                                    f"unnamed creatures fit only {names}, so at least {len(who) - len(keys)} of them must "
                                    "be a creature the creature list does not have yet. None of them is named until the "
                                    "evidence can tell them apart.")}
    return findings


def run(conn: sqlite3.Connection, now: datetime, source: str = "jwa-dashboard-feed",
        archive_creatures: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """(Re)work out every nameless code of `source`. Returns {source_id: creature key} for identified ones."""
    resolver = roster.resolver_for(conn)
    rows = {r["source_creature_id"]: r for r in conn.execute(
        "SELECT source_creature_id, display_name, rarity, match_method FROM source_creatures WHERE source = ?", (source,))}
    saved = {d["source_id"]: d for d in conn.execute("SELECT * FROM deductions WHERE source = ?", (source,))}
    codes = set(saved) | {i for i, r in rows.items() if r["match_method"] in ("unmatched", "deduced") and is_code(r["display_name"])}
    labels = _known_creatures(conn, resolver, source, archive_creatures)
    cutoff = OFFICIAL_LIST_VERSION
    listed = db.parse_iso(db.get_meta(conn, "roster_fetched_at"))
    open_codes: dict[str, dict[str, Any]] = {}
    for source_id in sorted(codes):
        row = rows.get(source_id)
        settled = source_id in resolver.alias_by_id.get(source, {}) or (
            row is not None and (row["match_method"] not in ("unmatched", "deduced") or not is_code(row["display_name"])))
        if settled:  # the feed names it now, or a hand-checked identification covers it
            conn.execute("DELETE FROM deductions WHERE source = ? AND source_id = ?", (source, source_id))
            continue
        before = saved.get(source_id)
        kept = json.loads(before["evidence"] or "{}") if before else {}
        label = row["display_name"].strip() if row else kept.get("label") or source_id[:8]
        rarity = row["rarity"] if row else kept.get("rarity")
        evidence = {**kept.get("snapshots", {}), **_snapshot_evidence(conn, source, source_id, label)}
        if before and not evidence:
            continue  # nothing to go on (an old conclusion whose snapshots are gone): leave it as it is
        open_codes[source_id] = {"label": label, "rarity": rarity, "evidence": evidence}

    # An earlier conclusion must not block a rival code: every open code is judged afresh, side by side.
    under_review = {c["label"].lower() for c in open_codes.values()}
    labels = {key: names - under_review for key, names in labels.items()}
    identified: dict[str, str] = {}
    for source_id, finding in _judge_together(resolver, labels, cutoff, open_codes, listed).items():
        row = rows.get(source_id)
        before = saved.get(source_id)
        label, rarity, evidence = (open_codes[source_id][k] for k in ("label", "rarity", "evidence"))
        possible = json.dumps(finding["possible"])
        unchanged = before and before["creature_key"] == finding["key"] and before["possible"] == possible
        with db.transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO deductions (source, source_id, creature_key, possible, note, decided_at, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, source_id, finding["key"], possible, finding["note"],
                 before["decided_at"] if unchanged else db.iso(now),
                 json.dumps({"label": label, "rarity": rarity, "snapshots": evidence}, sort_keys=True)),
            )
            if finding["key"] and row is not None:
                conn.execute("UPDATE source_creatures SET creature_key = ?, match_method = 'deduced' "
                             "WHERE source = ? AND source_creature_id = ?", (finding["key"], source, source_id))
            elif row is not None and row["match_method"] == "deduced":  # new evidence made it unclear again
                roster._ensure_unmatched_creature(conn, source, source_id, label, rarity, db.iso(now))
                conn.execute("UPDATE source_creatures SET creature_key = ?, match_method = 'unmatched' "
                             "WHERE source = ? AND source_creature_id = ?",
                             (roster.unresolved_key(source, source_id), source, source_id))
        if finding["key"]:
            identified[source_id] = finding["key"]
    return identified


def notes(conn: sqlite3.Connection) -> dict[str, str]:
    """Creature key -> explanation, for identified codes and for still-unidentified ones (keyed by placeholder)."""
    out: dict[str, str] = {}
    for d in conn.execute("SELECT * FROM deductions"):
        out[d["creature_key"] or roster.unresolved_key(d["source"], d["source_id"])] = d["note"]
    return out


def export(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {f"{d['source']}:{d['source_id']}": {"key": d["creature_key"], "possible": json.loads(d["possible"] or "[]"),
                                                "note": d["note"], "decided_at": d["decided_at"],
                                                "evidence": json.loads(d["evidence"] or "{}")}
            for d in conn.execute("SELECT * FROM deductions")}


def load(conn: sqlite3.Connection, saved: dict[str, dict[str, Any]]) -> None:
    """Put deductions kept elsewhere (the website archive) back into a fresh database."""
    with db.transaction(conn):
        for ident, d in saved.items():
            source, source_id = ident.split(":", 1)
            conn.execute(
                "INSERT OR IGNORE INTO deductions (source, source_id, creature_key, possible, note, decided_at, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, source_id, d.get("key"), json.dumps(d.get("possible") or []), d.get("note") or "",
                 d.get("decided_at") or "", json.dumps(d.get("evidence") or {})),
            )
