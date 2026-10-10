"""The full creature roster and the matching of source names to roster creatures.

Roster source: the Paleo.gg Dinodex page, which embeds a structured list of every
Jurassic World Alive creature (name, rarity, class, hybrid type, version added).
We read that one public page at most once a day. If it cannot be reached, the
last good copy (or the copy bundled with this project) is used instead.

Source feeds sometimes use odd names: truncated internal ids ("Tyrannoto"),
another language ("WOLLNASHORN"), or broken accents ("BR�NETTE"). The
resolver maps them to roster creatures using, in order: hand-checked aliases,
exact normalised names, unambiguous prefixes, and very close fuzzy matches. Every
match records how it was made; anything unmatched is kept and clearly labelled
instead of being guessed.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from . import config, db, net

log = logging.getLogger(__name__)

PALEO_DINODEX_URL = "https://www.paleo.gg/games/jurassic-world-alive/dinodex"
ROSTER_SOURCE = "paleo.gg dinodex"
SEED_FILE = config.BUNDLED_DATA_DIR / "roster_seed.json"
ALIAS_FILE = config.BUNDLED_DATA_DIR / "aliases.json"
RARITIES = ("common", "rare", "epic", "legendary", "unique", "apex", "omega")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


class RosterError(Exception):
    pass


# --- fetching ----------------------------------------------------------------------

def parse_paleo_dinodex(html: str) -> list[dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise RosterError("Paleo.gg Dinodex page layout changed (no embedded data found)")
    try:
        items = json.loads(match.group(1))["props"]["pageProps"]["dex"]["items"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RosterError(f"Paleo.gg Dinodex data layout changed ({exc})") from exc
    roster = []
    for item in items:
        if not isinstance(item, dict) or not item.get("key") or not item.get("name"):
            continue
        rarity = str(item.get("rarity") or "").lower() or None
        roster.append(
            {
                "key": str(item["key"]),
                "name": str(item["name"]).strip(),
                "rarity": rarity,
                "class": item.get("class"),
                "hybrid_type": item.get("hybrid_type"),
                "added_version": item.get("version"),
            }
        )
    if len(roster) < 200:
        raise RosterError(f"Paleo.gg Dinodex returned only {len(roster)} creatures; keeping previous roster")
    return roster


def fetch_paleo_roster(fetch: Callable[..., bytes] = net.fetch) -> list[dict[str, Any]]:
    html = fetch(PALEO_DINODEX_URL).decode("utf-8", errors="replace")
    return parse_paleo_dinodex(html)


def load_seed_roster() -> list[dict[str, Any]]:
    with open(SEED_FILE, encoding="utf-8") as fh:
        return json.load(fh)["creatures"]


def store_roster(conn: sqlite3.Connection, roster: list[dict[str, Any]], source: str, now: datetime) -> int:
    stamp = db.iso(now)
    with db.transaction(conn):
        for c in roster:
            conn.execute(
                """
                INSERT INTO creatures (key, name, rarity, class, hybrid_type, added_version,
                                       roster_source, in_roster, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (key) DO UPDATE SET
                    name = excluded.name, rarity = excluded.rarity, class = excluded.class,
                    hybrid_type = excluded.hybrid_type, added_version = excluded.added_version,
                    roster_source = excluded.roster_source, in_roster = 1, updated_at = excluded.updated_at
                """,
                (c["key"], c["name"], c.get("rarity"), c.get("class"), c.get("hybrid_type"),
                 c.get("added_version"), source, stamp),
            )
        db.set_meta(conn, "roster_source", source)
        db.set_meta(conn, "roster_count", str(len(roster)))
        db.set_meta(conn, "roster_updated_at", stamp)
    return len(roster)


def ensure_roster(conn: sqlite3.Connection, now: datetime) -> None:
    """Make sure some roster exists (bundled seed on first run)."""
    if db.creature_count(conn) == 0:
        store_roster(conn, load_seed_roster(), ROSTER_SOURCE + " (bundled copy)", now)


def refresh_roster_if_due(
    conn: sqlite3.Connection,
    now: datetime,
    fetch: Callable[..., bytes] = net.fetch,
    force: bool = False,
) -> str:
    ensure_roster(conn, now)
    last = db.parse_iso(db.get_meta(conn, "roster_fetched_at"))
    if not force and last and now - last < timedelta(hours=config.ROSTER_REFRESH_HOURS):
        return "Roster is up to date."
    roster = fetch_paleo_roster(fetch)
    count = store_roster(conn, roster, ROSTER_SOURCE, now)
    db.set_meta(conn, "roster_fetched_at", db.iso(now))
    fixed = reresolve_unmatched(conn, now)
    msg = f"Roster refreshed: {count} creatures."
    if fixed:
        msg += f" {fixed} previously unmatched creature name(s) are now identified."
    return msg


# --- name resolution -----------------------------------------------------------------

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]", "", text)


def clean_display_name(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.isupper():
        text = " ".join(w.capitalize() if not w.isdigit() else w for w in text.split(" "))
    return text


def unresolved_key(source: str, source_id: str) -> str:
    return f"unmatched-{normalize(source)[:12]}-{normalize(source_id)[:16]}"


@dataclass(frozen=True)
class Match:
    key: str | None
    method: str  # alias | exact | prefix | fuzzy | translated | unmatched


class Resolver:
    def __init__(self, roster: Iterable[dict[str, Any]], aliases: dict[str, Any] | None = None):
        self.roster = {c["key"]: c for c in roster}
        aliases = aliases if aliases is not None else load_aliases()
        self.alias_by_name = {normalize(k): v for k, v in aliases.get("by_name", {}).items()}
        self.alias_by_id = aliases.get("by_source_id", {})
        self.translations = [
            (re.compile(rf"\b{re.escape(german)}\b", re.IGNORECASE), english)
            for german, english in aliases.get("translations", {}).items()
            if not german.startswith("_")
        ]
        self.by_norm: dict[str, list[str]] = {}
        for key, c in self.roster.items():
            for variant in {normalize(c["name"]), normalize(key)}:
                if variant:
                    self.by_norm.setdefault(variant, []).append(key)

    def _pick(self, keys: list[str], rarity: str | None) -> str | None:
        keys = sorted(set(keys))
        if len(keys) == 1:
            return keys[0]
        if rarity:
            same = [k for k in keys if (self.roster[k].get("rarity") or "") == rarity]
            if len(same) == 1:
                return same[0]
        return None

    def resolve(self, source: str, source_id: str, display_name: str, rarity: str | None) -> Match:
        rarity = (rarity or "").lower()
        rarity = rarity if rarity in RARITIES else None
        by_id = self.alias_by_id.get(source, {}).get(source_id)
        if by_id and by_id in self.roster:
            return Match(by_id, "alias")
        match = self._resolve_name(display_name, rarity)
        if match.key:
            return match
        translated = display_name or ""
        for pattern, english in self.translations:
            translated = pattern.sub(english, translated)
        if translated != display_name:
            match = self._resolve_name(translated, rarity)
            if match.key:
                return Match(match.key, "translated")
        return Match(None, "unmatched")

    def _resolve_name(self, display_name: str, rarity: str | None) -> Match:
        norm = normalize(display_name)
        if not norm:
            return Match(None, "unmatched")
        alias = self.alias_by_name.get(norm)
        if alias and alias in self.roster:
            return Match(alias, "alias")
        if norm in self.by_norm:
            key = self._pick(self.by_norm[norm], rarity)
            if key:
                return Match(key, "exact")
        if len(norm) >= 6:
            starts = [k for n, ks in self.by_norm.items() if n.startswith(norm) for k in ks]
            key = self._pick(starts, rarity) if starts else None
            if key:
                return Match(key, "prefix")
        close = difflib.get_close_matches(norm, list(self.by_norm), n=3, cutoff=0.88)
        keys = [k for n in close for k in self.by_norm[n]]
        if rarity:
            keys = [k for k in keys if self.roster[k].get("rarity") == rarity]
        key = self._pick(keys, rarity) if keys else None
        if key:
            return Match(key, "fuzzy")
        return Match(None, "unmatched")


def load_aliases() -> dict[str, Any]:
    try:
        with open(ALIAS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def resolver_for(conn: sqlite3.Connection) -> Resolver:
    roster = [c for c in db.all_creatures(conn).values() if c["in_roster"]]
    return Resolver(roster)


def register_source_creatures(
    conn: sqlite3.Connection,
    source: str,
    creatures: Iterable[tuple[str, str, str | None]],
    now: datetime,
    resolver: Resolver | None = None,
) -> None:
    """Record (source_id, display_name, rarity) triples and map them to roster creatures.

    Must be called inside an open transaction.
    """
    resolver = resolver or resolver_for(conn)
    stamp = db.iso(now)
    for source_id, display_name, rarity in creatures:
        row = conn.execute(
            "SELECT creature_key, match_method, display_name FROM source_creatures "
            "WHERE source = ? AND source_creature_id = ?",
            (source, source_id),
        ).fetchone()
        if row and row["display_name"] == display_name:
            # Known name: keep the existing mapping. Unmatched names are retried
            # whenever the roster is refreshed (see reresolve_unmatched).
            conn.execute(
                "UPDATE source_creatures SET last_seen_at = ? WHERE source = ? AND source_creature_id = ?",
                (stamp, source, source_id),
            )
            continue
        match = resolver.resolve(source, source_id, display_name, rarity)
        key = match.key or _ensure_unmatched_creature(conn, source, source_id, display_name, rarity, stamp)
        conn.execute(
            """
            INSERT INTO source_creatures (source, source_creature_id, display_name, rarity,
                                          creature_key, match_method, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_creature_id) DO UPDATE SET
                display_name = excluded.display_name, rarity = excluded.rarity,
                creature_key = excluded.creature_key, match_method = excluded.match_method,
                last_seen_at = excluded.last_seen_at
            """,
            (source, source_id, display_name, rarity, key, match.method, stamp, stamp),
        )
        if match.method in ("prefix", "fuzzy"):
            log.info("Matched source name %r to %s (%s match)", display_name, key, match.method)
        elif match.method == "unmatched":
            log.warning("Could not identify creature %r (id %s) from %s", display_name, source_id, source)


def _ensure_unmatched_creature(conn, source, source_id, display_name, rarity, stamp) -> str:
    key = unresolved_key(source, source_id)
    name = clean_display_name(display_name)
    if not name or re.fullmatch(r"[0-9a-f]{6,}", name.lower()):
        name = f"Unidentified creature ({source_id[:8]})"
    conn.execute(
        """
        INSERT INTO creatures (key, name, rarity, class, hybrid_type, added_version,
                               roster_source, in_roster, updated_at)
        VALUES (?, ?, ?, NULL, NULL, NULL, 'unmatched source name', 0, ?)
        ON CONFLICT (key) DO UPDATE SET name = excluded.name, rarity = excluded.rarity,
                                        updated_at = excluded.updated_at
        """,
        (key, name, (rarity or "").lower() or None, stamp),
    )
    return key


def reresolve_unmatched(conn: sqlite3.Connection, now: datetime) -> int:
    resolver = resolver_for(conn)
    fixed = 0
    rows = conn.execute(
        "SELECT source, source_creature_id, display_name, rarity FROM source_creatures "
        "WHERE match_method = 'unmatched'"
    ).fetchall()
    with db.transaction(conn):
        for r in rows:
            match = resolver.resolve(r["source"], r["source_creature_id"], r["display_name"] or "", r["rarity"])
            if match.key:
                conn.execute(
                    "UPDATE source_creatures SET creature_key = ?, match_method = ? "
                    "WHERE source = ? AND source_creature_id = ?",
                    (match.key, match.method, r["source"], r["source_creature_id"]),
                )
                fixed += 1
    return fixed
