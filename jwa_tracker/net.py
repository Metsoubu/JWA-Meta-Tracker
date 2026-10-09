"""Small HTTP helper with retries for temporary failures."""
from __future__ import annotations

import gzip
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import config

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 30_000_000


class FetchError(Exception):
    """A download failed. `transient` says whether retrying later may help."""

    def __init__(self, message: str, *, transient: bool, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


def fetch(
    url: str,
    *,
    timeout: float = config.HTTP_TIMEOUT_SECONDS,
    attempts: int = config.HTTP_ATTEMPTS,
    backoff: tuple[float, ...] = config.HTTP_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Download `url`, retrying network errors, timeouts, 429 and 5xx responses."""
    last_error: FetchError | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Accept-Encoding": "gzip",
                    **(headers or {}),
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise FetchError(f"Response from {url} is unexpectedly large", transient=False)
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            transient = exc.code in (408, 425, 429) or exc.code >= 500
            last_error = FetchError(f"HTTP {exc.code} from {url}", transient=transient, status=exc.code)
            if not transient:
                raise last_error from exc
        except FetchError:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = FetchError(f"Network error contacting {url}: {reason}", transient=True)
        if attempt < attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0
            log.warning("%s - retrying in %ss (attempt %d of %d)", last_error, delay, attempt + 2, attempts)
            sleep(delay)
    assert last_error is not None
    raise last_error


def fetch_json(url: str, **kwargs: Any) -> Any:
    data = fetch(url, **kwargs)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"{url} did not return valid JSON ({exc})", transient=False) from exc
