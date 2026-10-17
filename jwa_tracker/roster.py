"""The full creature roster and the matching of source names to roster creatures.

Roster source: the Paleo.gg Dinodex page, which embeds a structured list of every
Jurassic World Alive creature (name, rarity, class, hybrid type, version added).
We read that one public page at most once a day. If it cannot be reached, the
last good copy (or the copy bundled with this project) is used instead.

Source feeds sometimes use odd names: truncated internal ids ("Tyrannoto"),
another language ("WOLLNASHORN"), or broken accents ("BR�NETTE"). The
resolver maps them to roster creatures using, in order: hand-checked aliases,
exact normalised names, the official names in other game languages (the Dinodex
in German, fetched with the roster), unambiguous prefixes, and very close fuzzy
matches. Every match records how it was made; anything still unmatched is kept and
clearly labelled (identify.py may later work out a nameless creature from evidence).
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
PALEO_DINODEX_LANG_URL = "https://www.paleo.gg/{lang}/games/jurassic-world-alive/dinodex"
# Game languages whose creature names the feed has been seen to use.
NAME_LANGUAGES = ("de",)
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
        for c in roster:
            for lang, name in (c.get("names") or {}).items():
                _store_name(conn, c["key"], lang, name)
    return len(roster)


def _store_name(conn: sqlite3.Connection, key: str, lang: str, name: str, replace: bool = True) -> None:
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    conn.execute(f"{verb} INTO creature_names (key, lang, name) VALUES (?, ?, ?)", (key, lang, name.strip()))


def fetch_localized_names(lang: str, fetch: Callable[..., bytes] = net.fetch) -> dict[str, str]:
    """Creature key -> its name in another game language, from the Dinodex in that language."""
    html = fetch(PALEO_DINODEX_LANG_URL.format(lang=lang)).decode("utf-8", errors="replace")
    return {c["key"]: c["name"] for c in parse_paleo_dinodex(html)}


def store_localized_names(conn: sqlite3.Connection, lang: str, names: dict[str, str]) -> None:
    with db.transaction(conn):
        for key, name in names.items():
            _store_name(conn, key, lang, name)


def ensure_roster(conn: sqlite3.Connection, now: datetime) -> None:
    """Make sure a roster exists and knows every creature in the bundled copy (the first run,
    or a program update that ships creatures newer than the last daily refresh)."""
    seed = load_seed_roster()
    if db.creature_count(conn) == 0:
        store_roster(conn, seed, ROSTER_SOURCE + " (bundled copy)", now)
        return
    known = {r["key"] for r in conn.execute("SELECT key FROM creatures")}
    missing = [c for c in seed if c["key"] not in known]
    named = {(r["key"], r["lang"]) for r in conn.execute("SELECT key, lang FROM creature_names")}
    unnamed = [(c["key"], lang, name) for c in seed for lang, name in (c.get("names") or {}).items()
               if (c["key"], lang) not in named]
    if missing or unnamed:
        with db.transaction(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO creatures (key, name, rarity, class, hybrid_type, added_version, "
                "roster_source, in_roster, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [(c["key"], c["name"], c.get("rarity"), c.get("class"), c.get("hybrid_type"), c.get("added_version"),
                  ROSTER_SOURCE + " (bundled copy)", db.iso(now)) for c in missing],
            )
            for key, lang, name in unnamed:
                _store_name(conn, key, lang, name, replace=False)


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
    for lang in NAME_LANGUAGES:
        try:
            store_localized_names(conn, lang, fetch_localized_names(lang, fetch))
        except (net.FetchError, RosterError) as exc:  # the English roster is what matters; names help
            log.warning("Could not refresh %s creature names: %s", lang, exc)
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
    method: str  # alias | exact | localized | prefix | fuzzy | translated | deduced | unmatched


class Resolver:
    def __init__(self, roster: Iterable[dict[str, Any]], aliases: dict[str, Any] | None = None,
                 deduced: dict[str, dict[str, str]] | None = None):
        self.roster = {c["key"]: c for c in roster}
        aliases = aliases if aliases is not None else load_aliases()
        self.alias_by_name = {normalize(k): v for k, v in aliases.get("by_name", {}).items()}
        self.alias_by_id = aliases.get("by_source_id", {})
        self.deduced_by_id = deduced or {}  # identify.py's conclusions: source -> {source id: key}
        # The same id aliases, keyed the way unidentified creatures were stored (for old history).
        self.unmatched_alias = {unresolved_key(src, sid): key
                                for ids_by_source in (self.deduced_by_id, self.alias_by_id)
                                for src, ids in ids_by_source.items() for sid, key in ids.items()}
        self.translations = [
            (re.compile(rf"\b{re.escape(german)}\b", re.IGNORECASE), english)
            for german, english in aliases.get("translations", {}).items()
            if not german.startswith("_")
        ]
        self.by_norm: dict[str, list[str]] = {}
        self.by_local_name: dict[str, list[str]] = {}
        for key, c in self.roster.items():
            for variant in {normalize(c["name"]), normalize(key)}:
                if variant:
                    self.by_norm.setdefault(variant, []).append(key)
            for name in (c.get("names") or {}).values():
                if normalize(name):
                    self.by_local_name.setdefault(normalize(name), []).append(key)

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
        deduced = self.deduced_by_id.get(source, {}).get(source_id)
        if deduced and deduced in self.roster:
            return Match(deduced, "deduced")
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
        if norm in self.by_local_name:
            key = self._pick(self.by_local_name[norm], rarity)
            if key:
                return Match(key, "localized")
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


def identified_notes() -> dict[str, str]:
    """Why a creature the feed sends without a name was identified (creature key -> explanation)."""
    return {k: v for k, v in load_aliases().get("identified", {}).items() if not k.startswith("_")}


def resolver_for(conn: sqlite3.Connection) -> Resolver:
    roster = [dict(c) for c in db.all_creatures(conn).values() if c["in_roster"]]
    names: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT key, lang, name FROM creature_names"):
        names.setdefault(r["key"], {})[r["lang"]] = r["name"]
    for c in roster:
        c["names"] = names.get(c["key"], {})
    deduced: dict[str, dict[str, str]] = {}
    for d in conn.execute("SELECT source, source_id, creature_key FROM deductions WHERE creature_key IS NOT NULL"):
        deduced.setdefault(d["source"], {})[d["source_id"]] = d["creature_key"]
    return Resolver(roster, deduced=deduced)


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
