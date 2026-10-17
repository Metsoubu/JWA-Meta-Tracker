"""Work out which creature a nameless feed code is, from the feed's own evidence.

The feed's name list lags behind the game: creatures newer than it arrive with only
a code (e.g. "2b88a0f8") and no name or picture. For every such code this module
narrows the creature list down:

- rarity: the feed's own rarity if it gives one; otherwise the tournament's allowed
  rarities, the lowest level seen (no creature is below its rarity's starting level)
  and omega training points (only omega creatures have them);
- newness: a nameless creature is newer than the feed's name list, so only creatures
  from the newest game version the feed names properly, or later, qualify; creatures
  the feed already names are ruled out;
- alphabetical place: tournaments list their eligible creatures alphabetically by
  English name, so where the code sits between two named creatures, its name must
  fall between theirs.

If exactly one creature is left it is used, and its page says it was worked out
automatically and why. A brand-new creature can reach the feed before the creature
list (Paleo.gg) has it, so nothing is named until the list has been refreshed at
least GRACE_DAYS after the code first appeared; until then the page shows the
possibilities. If a few are left, the page lists them as possibilities and
the creature stays "Unidentified". Nothing is guessed beyond that, and the result is
re-checked on every update as new evidence arrives.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import db, roster

START_LEVEL = {"common": 1, "rare": 6, "epic": 11, "legendary": 16, "unique": 21, "apex": 26, "omega": 1}
CODE = re.compile(r"^[0-9a-f]{8}$")
NAMED_METHODS = ("exact", "localized", "translated")  # the feed knew the creature's real name
MAX_POSSIBILITIES = 4
GRACE_DAYS = 3


def is_code(label: str | None) -> bool:
    return bool(CODE.match((label or "").strip().lower()))


def version_key(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").lstrip("v").split("."))
    except ValueError:
        return (0,)


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def find(conn: sqlite3.Connection, resolver: roster.Resolver, source: str, source_id: str,
         label: str, rarity_label: str | None) -> dict[str, Any]:
    """{"key": creature key or None, "possible": [keys], "note": explanation} for one nameless code."""
    cast = resolver.roster
    rows = conn.execute(
        "SELECT source_creature_id, creature_key, match_method FROM source_creatures WHERE source = ?", (source,)
    ).fetchall()
    taken = {r["creature_key"] for r in rows if r["match_method"] != "unmatched" and r["source_creature_id"] != source_id}
    named_versions = [cast[r["creature_key"]].get("added_version") for r in rows
                      if r["match_method"] in NAMED_METHODS and r["creature_key"] in cast]
    cutoff = max(named_versions, key=version_key) if named_versions else None

    occurrences = conn.execute(
        """
        SELECT t.snapshot_id, s.leaderboard_name, s.captured_at, b.level, b.omega
        FROM team_members m
        JOIN teams t ON t.id = m.team_id
        JOIN snapshots s ON s.id = t.snapshot_id
        LEFT JOIN member_builds b ON b.team_id = m.team_id AND b.slot = m.slot
        WHERE s.source = ? AND m.source_creature_id = ?
        """,
        (source, source_id),
    ).fetchall()
    rules = db.build_rules(conn)
    first_seen = min((o["captured_at"] for o in occurrences), default=None)

    reasons: list[str] = []
    rarity = (rarity_label or "").lower()
    if rarity in START_LEVEL:
        possible = {rarity}
        reasons.append(f"the data source lists it as {rarity.title()}")
    else:
        possible = set(START_LEVEL)
        for leaderboard, allowed in sorted({(o["leaderboard_name"], tuple(rules.get(o["snapshot_id"], {}).get("rarities") or ()))
                                            for o in occurrences}):
            if allowed:
                possible &= {a.lower() for a in allowed}
                reasons.append(f"{leaderboard} allowed only {_join(list(allowed))} creatures")
        levels = [o["level"] for o in occurrences if o["level"] is not None]
        if levels:
            lowest = min(levels)
            possible = {r for r in possible if START_LEVEL[r] <= lowest}
            reasons.append(f"its lowest level is {lowest:g}")
        if any(o["omega"] and json.loads(o["omega"]) for o in occurrences):
            possible &= {"omega"}
            reasons.append("it has omega training")
        elif levels:
            possible.discard("omega")
            reasons.append(f"none of its {len(levels)} copies has omega training")

    candidates = [c for c in cast.values()
                  if c.get("rarity") in possible and c["key"] not in taken
                  and (cutoff is None or version_key(c.get("added_version")) >= version_key(cutoff))]
    newness = (f"the data source names every creature it knows up to game version {cutoff.lstrip('v')}, so a "
               f"creature without any name is from that version or later") if cutoff else ""

    slot = None
    for o in {o["snapshot_id"]: o for o in occurrences}.values():
        eligible = rules.get(o["snapshot_id"], {}).get("eligible") or []
        if label not in eligible:
            continue
        i = eligible.index(label)
        english = []
        for neighbours in (reversed(eligible[:i]), eligible[i + 1:]):
            found = None
            for name in neighbours:
                if is_code(name):
                    continue
                match = resolver.resolve(source, "", name, None)
                if match.key:
                    found = cast[match.key]["name"]
                break
            english.append(found)
        before, after = english
        if before and after and roster.normalize(before) < roster.normalize(after):
            candidates = [c for c in candidates
                          if roster.normalize(before) < roster.normalize(c["name"]) < roster.normalize(after)]
            slot = (o["leaderboard_name"], before, after)
            break

    candidates.sort(key=lambda c: (version_key(c.get("added_version")), c["name"]))
    intro = f"The data source gives no name for this creature (code {label}"
    intro += f", first seen {first_seen[:10]})." if first_seen else ")."
    evidence = [r for r in reasons]
    if newness:
        evidence.append(newness)
    if slot:
        evidence.append(f"in {slot[0]}'s alphabetical list of eligible creatures it sits between {slot[1]} and {slot[2]}")
    why = ("; ".join(evidence) + ".") if evidence else ""
    listed = db.parse_iso(db.get_meta(conn, "roster_fetched_at"))
    waiting = first_seen is not None and (listed is None or listed < db.parse_iso(first_seen) + timedelta(days=GRACE_DAYS))
    if waiting and candidates:
        names = _join([c["name"] for c in candidates[:MAX_POSSIBILITIES]]).replace(" and ", " or ")
        more = " (or another creature)" if len(candidates) > MAX_POSSIBILITIES else ""
        note = (f"{intro} So far it fits {names}{more}. It is named only once the creature list has had "
                f"{GRACE_DAYS} days to add brand-new creatures, so it is not mistaken for an older one.")
        return {"key": None, "possible": [c["key"] for c in candidates[:MAX_POSSIBILITIES]], "note": note}
    if len(candidates) == 1:
        c = candidates[0]
        note = (f"{intro} Worked out automatically as {c['name']} ({c.get('rarity', '').title()}, game version "
                f"{(c.get('added_version') or '?').lstrip('v')}): {why[:1].lower() + why[1:]} {c['name']} is the only "
                f"creature that fits and is not already named by the data source.")
        return {"key": c["key"], "possible": [c["key"]], "note": note.replace("  ", " ")}
    if 1 < len(candidates) <= MAX_POSSIBILITIES:
        names = [c["name"] for c in candidates]
        note = (f"{intro} It is probably {_join(names).replace(' and ', ' or ')}, but the evidence so far cannot tell "
                f"which: {why[:1].lower() + why[1:]}")
        return {"key": None, "possible": [c["key"] for c in candidates], "note": note}
    return {"key": None, "possible": [], "note": f"{intro} There is not enough evidence yet to tell which creature it is."}


def run(conn: sqlite3.Connection, now: datetime, source: str = "jwa-dashboard-feed") -> dict[str, str]:
    """(Re)work out every nameless code of `source`. Returns {source_id: creature key} for identified ones."""
    resolver = roster.resolver_for(conn)
    rows = conn.execute(
        "SELECT source_creature_id, display_name, rarity, creature_key, match_method FROM source_creatures "
        "WHERE source = ? AND match_method IN ('unmatched', 'deduced')", (source,)
    ).fetchall()
    identified: dict[str, str] = {}
    for r in rows:
        if not is_code(r["display_name"]):
            continue
        finding = find(conn, resolver, source, r["source_creature_id"], r["display_name"].strip(), r["rarity"])
        placeholder = roster.unresolved_key(source, r["source_creature_id"])
        with db.transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO deductions (source, source_id, creature_key, possible, note, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, r["source_creature_id"], finding["key"], json.dumps(finding["possible"]), finding["note"],
                 db.iso(now)),
            )
            if finding["key"]:
                conn.execute("UPDATE source_creatures SET creature_key = ?, match_method = 'deduced' "
                             "WHERE source = ? AND source_creature_id = ?", (finding["key"], source, r["source_creature_id"]))
                identified[r["source_creature_id"]] = finding["key"]
            elif r["match_method"] == "deduced":  # new evidence made it unclear again: back to unidentified
                roster._ensure_unmatched_creature(conn, source, r["source_creature_id"], r["display_name"], r["rarity"],
                                                  db.iso(now))
                conn.execute("UPDATE source_creatures SET creature_key = ?, match_method = 'unmatched' "
                             "WHERE source = ? AND source_creature_id = ?", (placeholder, source, r["source_creature_id"]))
    return identified


def notes(conn: sqlite3.Connection) -> dict[str, str]:
    """Creature key -> explanation, for identified codes and for still-unidentified ones (keyed by placeholder)."""
    out: dict[str, str] = {}
    for d in conn.execute("SELECT * FROM deductions"):
        key = d["creature_key"] or roster.unresolved_key(d["source"], d["source_id"])
        out[key] = d["note"]
    return out


def export(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {f"{d['source']}:{d['source_id']}": {"key": d["creature_key"], "possible": json.loads(d["possible"] or "[]"),
                                                "note": d["note"], "decided_at": d["decided_at"]}
            for d in conn.execute("SELECT * FROM deductions")}


def load(conn: sqlite3.Connection, saved: dict[str, dict[str, Any]]) -> None:
    """Put deductions kept elsewhere (the website archive) back into a fresh database."""
    with db.transaction(conn):
        for ident, d in saved.items():
            source, source_id = ident.split(":", 1)
            conn.execute(
                "INSERT OR IGNORE INTO deductions (source, source_id, creature_key, possible, note, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, source_id, d.get("key"), json.dumps(d.get("possible") or []), d.get("note") or "",
                 d.get("decided_at") or ""),
            )
