"""Private local dashboard server (http://127.0.0.1 only).

Serves the static dashboard (web/) plus data/site.json, which is built from your
database. The same dashboard files also work as a static website (see site.py).
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import collector, config, db, images, scheduler, site, standins

log = logging.getLogger(__name__)

_DETAILS_PATH = re.compile(r"^/data/details/(\d{4}-\d{2})\.json$")
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, db_file: Path, demo: bool = False):
        self.db_file = db_file
        self.demo = demo
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._cache_lock = threading.Lock()
        self._cache: tuple[Any, bytes] | None = None
        self._details_cache: dict[str, tuple[Any, bytes]] = {}
        super().__init__((config.HOST, port), Handler)

    def site_json(self, conn) -> bytes:
        """data/site.json, rebuilt only when the database or pictures changed."""
        key = _state_key(conn)
        with self._cache_lock:
            if self._cache and self._cache[0] == key:
                return self._cache[1]
            payload = site.build_local(conn, db.utcnow(), demo=self.demo)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._cache = (key, body)
            return body

    def details_json(self, conn, month: str) -> bytes:
        """data/details/<month>.json: that month's teams with builds, for the creature panel."""
        key = (_state_key(conn), conn.execute("SELECT COUNT(*) FROM member_builds").fetchone()[0])
        with self._cache_lock:
            cached = self._details_cache.get(month)
            if cached and cached[0] == key:
                return cached[1]
            body = json.dumps(site.details_month_local(conn, month), ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            self._details_cache[month] = (key, body)
            return body


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _state_key(conn) -> tuple:
    snaps = conn.execute("SELECT MAX(id), COUNT(*) FROM snapshots").fetchone()
    run = conn.execute("SELECT id, status, finished_at FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
    mapping = conn.execute(
        "SELECT COUNT(*), SUM(LENGTH(creature_key)), MAX(last_seen_at) FROM source_creatures"
    ).fetchone()
    roster_stamp = db.get_meta(conn, "roster_updated_at")
    files = tuple(_mtime(p) for p in (images.CREDITS_FILE, standins.STANDIN_FILE, standins.BODY_TYPES_FILE,
                                      images.CUSTOM_DIR))
    return tuple(snaps), tuple(run) if run else None, tuple(mapping), roster_stamp, files


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "JWAMetaTracker"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        log.debug("%s - %s", self.address_string(), fmt % args)

    # --- plumbing --------------------------------------------------------------------

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.server.allowed_hosts

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _conn(self):
        return db.connect(self.server.db_file, initialize=False)

    # --- routing -----------------------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.BAD_REQUEST, "unexpected Host header")
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/health":
                return self._json({"app": config.APP_ID, "version": config.APP_VERSION, "demo": self.server.demo})
            if path == "/api/status":
                return self._status()
            if path == "/data/site.json":
                conn = self._conn()
                try:
                    return self._send(HTTPStatus.OK, self.server.site_json(conn), "application/json; charset=utf-8")
                finally:
                    conn.close()
            month = _DETAILS_PATH.match(path)
            if month:
                conn = self._conn()
                try:
                    body = self.server.details_json(conn, month.group(1))
                    return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                finally:
                    conn.close()
            if path.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return self._static(path)
        except Exception as exc:  # noqa: BLE001 - never crash the server thread silently
            log.exception("Request failed: %s", self.path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {site.sanitize(str(exc))}")

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.BAD_REQUEST, "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc not in self.server.allowed_hosts:
            return self._error(HTTPStatus.FORBIDDEN, "cross-site request refused")
        if self.headers.get("X-JWA-Tracker") != "1":
            return self._error(HTTPStatus.FORBIDDEN, "missing request header")
        if urllib.parse.urlsplit(self.path).path == "/api/collect":
            return self._start_collection()
        if urllib.parse.urlsplit(self.path).path == "/api/shutdown":
            # START.bat uses this to replace a dashboard left open from an older version.
            self._json({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _status(self) -> None:
        """Small change-detector for the page: no paths, user names or computer settings."""
        conn = self._conn()
        try:
            snap = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
            run = conn.execute(
                "SELECT id, status, finished_at FROM collection_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        running = False if self.server.demo else collector.is_collection_running()
        return self._json({
            "latest_snapshot_id": snap,
            "last_run": dict(run) if run else None,
            "collection_running": running,
            "demo": self.server.demo,
        })

    def _start_collection(self) -> None:
        if self.server.demo:
            return self._error(HTTPStatus.CONFLICT, "Updates are disabled while viewing example data.")
        if collector.is_collection_running():
            return self._json({"started": False, "message": "An update is already running."})
        spawn_background_collection("manual")
        return self._json({"started": True, "message": "Update started."})

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        web_root = config.WEB_DIR.resolve()
        if web_root not in target.parents or not target.is_file() or target.name in ("credits.json", "standins.json"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml", "application/json"):
            ctype += "; charset=utf-8"
        extra = {"Content-Security-Policy": CSP}
        if target.suffix == ".svg":
            extra["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        return self._send(HTTPStatus.OK, target.read_bytes(), ctype, extra)


mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/webp", ".webp")


def spawn_background_collection(trigger: str) -> None:
    """Start a collection in a separate, windowless process."""
    exe = scheduler.pythonw_path() if os.name == "nt" else Path(sys.executable)
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    subprocess.Popen(
        [str(exe), str(config.ENTRY_SCRIPT), "collect", "--trigger", trigger],
        cwd=str(config.PROJECT_DIR),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def probe(port: int) -> dict[str, Any] | None:
    """If our own dashboard already runs on this port, return its health info."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if data.get("app") == config.APP_ID else None
    except (OSError, ValueError):
        return None


def request_shutdown(port: int, wait_seconds: float = 6.0) -> bool:
    """Ask our own dashboard on this port to stop; True once it no longer answers."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST",
                                 headers={"X-JWA-Tracker": "1"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if probe(port) is None:
            return True
        time.sleep(0.2)
    return False


def make_server(db_file: Path, demo: bool = False, preferred: int = config.DEFAULT_PORT) -> DashboardServer:
    last_error: OSError | None = None
    for port in range(preferred, preferred + config.PORT_SEARCH_RANGE):
        try:
            return DashboardServer(port, db_file, demo)
        except OSError as exc:
            last_error = exc
    raise OSError(f"No free port between {preferred} and {preferred + config.PORT_SEARCH_RANGE - 1}: {last_error}")


def serve_in_thread(db_file: Path, demo: bool = False, port: int = 0) -> DashboardServer:
    """Used by tests: start on an ephemeral port in a background thread."""
    srv = DashboardServer(port, db_file, demo)
    actual = srv.server_address[1]
    srv.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}"}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
