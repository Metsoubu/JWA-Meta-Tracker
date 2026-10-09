"""Openly licensed creature silhouettes from PhyloPic (https://www.phylopic.org).

PhyloPic hosts silhouettes of real organisms under Creative Commons licences
(CC0, public domain, CC BY, CC BY-SA, ...). For each roster creature that is a
real, non-hybrid animal, we look up its genus/species and save the silhouette
with its licence and attribution in credits.json, shown on the dashboard's
About page. Hybrids and film characters reuse the silhouette of the animal
whose body shape they share (see standins.py).

Official game artwork is owned by Ludia/Universal and is not used or bundled.
You can add your own pictures in web/img/custom/<creature_key>.png (or .jpg,
.webp, .svg) and the dashboard will prefer them.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import config, db, net

log = logging.getLogger(__name__)

API = "https://api.phylopic.org"
ACCEPT = {"Accept": "application/vnd.phylopic.v2+json"}
CREDITS_FILE = config.SILHOUETTE_DIR / "credits.json"
CUSTOM_DIR = config.WEB_DIR / "img" / "custom"
CUSTOM_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg")

# Names of film/series characters: their names are not scientific names.
CHARACTER_NAMES = {
    "blue", "ghost", "red", "tiger", "panthera", "bumpy", "junior", "big", "clever", "blonde",
    "blondie", "brunette", "beta", "delta", "echo", "charlie", "rexy", "mutadon",
}
# Characters whose species is well established in the films/series.
TAXON_OVERRIDES = {
    "93_classic_t_rex": "tyrannosaurus rex",
    "big_eatie": "tyrannosaurus",
    "blue": "velociraptor",
    "clever_girl": "velociraptor",
    "bumpy": "ankylosaurus",
    "ghost": "atrociraptor",
    "red": "atrociraptor",
    "tiger": "atrociraptor",
    "panthera": "atrociraptor",
    "tyrannosaur_buck": "tyrannosaurus",
    "tyrannosaur_doe": "tyrannosaurus",
}
# PhyloPic returns an unrelated organism for these names (a fish, a polyp).
NO_SILHOUETTE = {"scaphognathus", "hydra_boa"}
_STRIP_WORDS = re.compile(r"\b(gen\s*2|gen2|mattel|boss|rebirth|lux|classic|alpha)\b", re.I)
_UNSAFE_SVG = re.compile(r"<\s*script|javascript:|\son[a-z]+\s*=|<\s*foreignObject|<!ENTITY", re.I)


def load_credits() -> dict[str, Any]:
    try:
        with open(CREDITS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_credits(credits: dict[str, Any]) -> None:
    write_json(CREDITS_FILE, credits)


def write_json(path, data: Any) -> None:
    """Write a JSON file, retrying briefly: cloud-sync tools (OneDrive) can lock it for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True)
    tmp = path.with_suffix(".tmp")
    for attempt in range(10):
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5 * (attempt + 1))


def taxon_queries(creature: dict[str, Any]) -> list[str]:
    """Scientific-name queries to try for a creature, most specific first."""
    if creature.get("key") in NO_SILHOUETTE:
        return []
    override = TAXON_OVERRIDES.get(creature.get("key", ""))
    if override:
        return [override]
    if creature.get("hybrid_type") != "non_hybrid":
        return []
    if re.match(r"^\W*\d", creature.get("name") or ""):
        return []
    name = _STRIP_WORDS.sub(" ", creature.get("name") or "")
    words = [w for w in re.split(r"[^A-Za-z]+", name) if w]
    if not words or words[0].lower() in CHARACTER_NAMES:
        return []
    queries = []
    if len(words) >= 2:
        queries.append(f"{words[0]} {words[1]}".lower())
    queries.append(words[0].lower())
    return queries


def licence_name(url: str) -> str:
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/([\d.]+)", url or "")
    if not m:
        return url or "unknown"
    kind, code, ver = m.groups()
    if kind == "publicdomain":
        return {"zero": f"CC0 {ver}", "mark": f"Public Domain Mark {ver}"}.get(code, f"Public domain ({code})")
    return f"CC {code.upper()} {ver}"


class PhyloPic:
    def __init__(self, fetch: Callable[..., bytes] = net.fetch, fetch_json: Callable[..., Any] = net.fetch_json):
        self._fetch = fetch
        self._fetch_json = fetch_json
        self._build: int | None = None

    def build(self) -> int:
        if self._build is None:
            doc = self._fetch_json(f"{API}/", headers=ACCEPT, attempts=2)
            self._build = int(doc["build"])
        return self._build

    def lookup(self, query: str) -> dict[str, Any] | None:
        from urllib.parse import quote

        url = (
            f"{API}/nodes?build={self.build()}&filter_name={quote(query)}"
            "&embed_items=true&embed_primaryImage=true&page=0"
        )
        try:
            doc = self._fetch_json(url, headers=ACCEPT, attempts=2)
        except net.FetchError as exc:
            if exc.status == 404:  # PhyloPic answers 404 when nothing matches
                return None
            raise
        for item in doc.get("_embedded", {}).get("items", []):
            image = item.get("_embedded", {}).get("primaryImage")
            if not image:
                continue
            links = image.get("_links", {})
            vector = links.get("vectorFile", {}).get("href")
            licence = links.get("license", {}).get("href")
            if not vector or not licence:
                continue
            names = item.get("names") or [[]]
            taxon = " ".join(part.get("text", "") for part in names[0] if part.get("class") == "scientific")
            taxon = taxon or " ".join(part.get("text", "") for part in names[0])
            return {
                "taxon": taxon.strip(),
                "vector": vector,
                "license": licence,
                "license_name": licence_name(licence),
                "attribution": image.get("attribution") or links.get("contributor", {}).get("title") or "",
                "page": f"https://www.phylopic.org/images/{image.get('uuid', '')}",
            }
        return None

    def download_svg(self, url: str) -> bytes:
        if not url.startswith("https://images.phylopic.org/"):
            raise ValueError(f"unexpected image host in {url}")
        data = self._fetch(url, attempts=2)
        if len(data) > 3_000_000:
            raise ValueError("silhouette file too large")
        text = data.decode("utf-8", errors="replace")
        if "<svg" not in text or _UNSAFE_SVG.search(text):
            raise ValueError("silhouette file is not a plain SVG image")
        return data


def fetch_missing_silhouettes(
    conn: sqlite3.Connection,
    limit: int | None = 25,
    client: PhyloPic | None = None,
    pause: float = 0.3,
) -> int:
    """Look up silhouettes for roster creatures not yet checked. Returns how many were added."""
    credits = load_credits()
    creatures = [c for c in db.all_creatures(conn).values() if c["in_roster"]]
    todo = [c for c in creatures if c["key"] not in credits and taxon_queries(c)]
    if not todo:
        return 0
    client = client or PhyloPic()
    config.SILHOUETTE_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    checked = 0
    for creature in todo:
        if limit is not None and checked >= limit:
            break
        checked += 1
        key = creature["key"]
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            hit = None
            for query in taxon_queries(creature):
                hit = client.lookup(query)
                if hit:
                    hit["query"] = query
                    break
            if not hit:
                credits[key] = {"status": "none", "checked_at": stamp}
                continue
            svg = client.download_svg(hit["vector"])
            filename = f"{re.sub(r'[^a-z0-9_]', '_', key.lower())}.svg"
            (config.SILHOUETTE_DIR / filename).write_bytes(svg)
            credits[key] = {"status": "ok", "file": filename, "checked_at": stamp, **{k: v for k, v in hit.items() if k != "vector"}}
            added += 1
        except (net.FetchError, ValueError, KeyError, OSError) as exc:
            log.warning("Silhouette lookup for %s failed: %s", key, exc)
            if isinstance(exc, net.FetchError) and exc.transient:
                continue  # try again next time
            credits[key] = {"status": "error", "checked_at": stamp, "error": str(exc)}
        finally:
            if checked % 20 == 0:
                save_credits(credits)
        time.sleep(pause)
    save_credits(credits)
    return added


def custom_images() -> dict[str, str]:
    """User-supplied pictures: creature key -> URL path."""
    found: dict[str, str] = {}
    if CUSTOM_DIR.is_dir():
        for path in CUSTOM_DIR.iterdir():
            if path.suffix.lower() in CUSTOM_EXTENSIONS and path.is_file():
                found[path.stem.lower()] = f"img/custom/{path.name}"
    return found
