"""Body-type stand-in pictures for creatures that have no silhouette of their own.

Hybrids (and a few film characters) are not real animals, so PhyloPic has no
picture of them. Instead we show the silhouette of the real animal they are
built from: Paleo.gg's page for each creature lists its fusion ingredients all
the way down to the base creatures. We follow the ingredient whose name the
hybrid shares (Baryotor -> Baryosaurus -> Baryothus -> Leucistic Baryonyx ->
Baryonyx), falling back to any ancestor that has a silhouette. The result is a
body-type icon (theropod, sauropod, snake, flyer, ...), credited to the
original silhouette's author. Results are saved in standins.json so each
creature's page is fetched only once.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config, db, images, net, roster

log = logging.getLogger(__name__)

STANDIN_FILE = config.SILHOUETTE_DIR / "standins.json"
BODY_TYPES_FILE = config.BUNDLED_DATA_DIR / "body_types.json"
PALEO_DETAIL_URL = "https://www.paleo.gg/games/jurassic-world-alive/dinodex/{key}"
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_MODIFIER = re.compile(r"^(leucistic|rebirth|alpha|classic|93_classic|dark|ghost)_|_(gen_?2|mattel|lux|boss|\d)$")


def _load_body_types() -> dict[str, str]:
    """Hand-checked body types: creature key -> creature whose silhouette matches its shape."""
    try:
        with open(BODY_TYPES_FILE, encoding="utf-8") as fh:
            return dict(json.load(fh)["map"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}


BODY_OVERRIDES = _load_body_types()


def load() -> dict[str, Any]:
    try:
        with open(STANDIN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(data: dict[str, Any]) -> None:
    images.write_json(STANDIN_FILE, data)


def _variants(key: str):
    seen = set()
    while key and key not in seen:
        seen.add(key)
        yield key
        key = _MODIFIER.sub("", key)


def _name(key: str) -> str:
    base = key
    for base in _variants(key):
        pass
    return roster.normalize(base)


def _prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def own_silhouette(key: str, credits: dict[str, Any]) -> str | None:
    """The creature (or its plain variant / body-type override) that has a silhouette."""
    for variant in _variants(key):
        if credits.get(variant, {}).get("status") == "ok":
            return variant
        override = BODY_OVERRIDES.get(variant)
        if override and credits.get(override, {}).get("status") == "ok":
            return override
    return None


def picture_index(keys) -> dict[str, dict[str, Any]]:
    """For each creature key, the picture to show: {"url", "kind", "credit"} (missing = use initials).

    Order: your own picture (web/img/custom) > its own silhouette > hand-checked
    body type > automatic stand-in from fusion ingredients.
    """
    credits = images.load_credits()
    found_standins = load()
    custom = images.custom_images()
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        if key in custom:
            out[key] = {"url": custom[key], "kind": "custom", "credit": "Your own picture"}
            continue
        via = own_silhouette(key, credits)
        if not via:
            entry = found_standins.get(key, {})
            if entry.get("status") == "ok" and credits.get(entry.get("via"), {}).get("status") == "ok":
                via = entry["via"]
        if not via:
            continue
        c = credits[via]
        source = f"{c.get('taxon', '')} silhouette by {c.get('attribution') or 'unknown'}, {c.get('license_name', '')} (PhyloPic)"
        out[key] = {
            "url": f"img/silhouettes/{c['file']}",
            "kind": "silhouette" if via == key else "body-type",
            "credit": source if via == key else f"Body-type icon: {source}",
        }
    return out


def parse_tree(html: str, key: str) -> dict[str, list[str]]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("creature page layout changed (no embedded data)")
    detail = json.loads(match.group(1))["props"]["pageProps"].get("detail") or {}
    tree = {
        k: [i for i in (v or {}).get("ingredients", []) if isinstance(i, str)]
        for k, v in (detail.get("evolutionData") or {}).items()
    }
    tree.setdefault(key, [i for i in detail.get("ingredients") or [] if isinstance(i, str)])
    return tree


def resolve(key: str, tree: dict[str, list[str]], credits: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Walk the fusion tree to the closest ancestor with a silhouette."""
    seen: set[str] = set()

    def visit(node: str, path: list[str]):
        if node in seen or len(path) > 10:
            return None
        seen.add(node)
        hit = own_silhouette(node, credits)
        if hit:
            return hit, path
        kids = tree.get(node) or []
        ranked = sorted(kids, key=lambda k: -_prefix_len(_name(node), _name(k)))  # stable: ties keep order
        for kid in ranked:
            found = visit(kid, path + [kid])
            if found:
                return found
        return None

    found = visit(key, [key])
    return (found[0], found[1]) if found else (None, [key])


def update(
    conn: sqlite3.Connection,
    keys: list[str],
    fetch: Callable[..., bytes] = net.fetch,
    limit: int | None = 15,
    pause: float = 1.0,
) -> int:
    """Find stand-ins for the given creatures that have no picture yet. Returns how many were added."""
    credits = images.load_credits()
    data = load()
    creatures = db.all_creatures(conn)
    added = 0
    fetched = 0
    for key in keys:
        if key in data or own_silhouette(key, credits) or not _KEY_RE.match(key):
            continue
        info = creatures.get(key)
        if not info or not info["in_roster"]:
            continue
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tree: dict[str, list[str]] = {key: []}
        if (info.get("hybrid_type") or "non_hybrid") != "non_hybrid":
            if limit is not None and fetched >= limit:
                break
            try:
                tree = parse_tree(fetch(PALEO_DETAIL_URL.format(key=key), attempts=2).decode("utf-8", "replace"), key)
            except (net.FetchError, ValueError, KeyError, json.JSONDecodeError) as exc:
                log.warning("Could not read fusion ingredients for %s: %s", key, exc)
                if isinstance(exc, net.FetchError) and exc.transient:
                    continue
                data[key] = {"status": "none", "checked_at": stamp, "error": str(exc)}
                continue
            finally:
                fetched += 1
                time.sleep(pause)
        via, path = resolve(key, tree, credits)
        if via:
            data[key] = {"status": "ok", "via": via, "path": path, "checked_at": stamp}
            added += 1
        else:
            data[key] = {"status": "none", "path": path, "checked_at": stamp}
    save(data)
    return added


def used_creature_keys(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT creature_key FROM source_creatures ORDER BY creature_key")]
