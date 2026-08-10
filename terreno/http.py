"""Shared HTTP client. Every source goes through this — polite rate limiting,
retries, and a per-host backoff live here once rather than in five scrapers.
"""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from urllib.parse import urlsplit

import requests

log = logging.getLogger("terreno.http")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# Seconds to wait between requests to the same host.
MIN_INTERVAL = 1.5

_last_hit: dict[str, float] = defaultdict(float)
_blocked: set[str] = set()

_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)


def _throttle(host: str) -> None:
    elapsed = time.monotonic() - _last_hit[host]
    wait = MIN_INTERVAL - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.4))
    _last_hit[host] = time.monotonic()


def get(url: str, *, params: dict | None = None, headers: dict | None = None,
        timeout: int = 25, retries: int = 3, json: bool = False):
    """GET with backoff. Returns Response, parsed JSON, or None.

    Never raises for a dead source: a failing portal degrades that source to
    zero results and is logged, rather than taking the whole run down.
    """
    host = urlsplit(url).netloc
    if host in _blocked:
        return None

    for attempt in range(retries):
        _throttle(host)
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("%s: %s (attempt %d)", host, exc, attempt + 1)
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 200:
            if json:
                try:
                    return r.json()
                except ValueError:
                    log.warning("%s: response was not JSON", host)
                    return None
            return r

        if r.status_code in (403, 429):
            log.warning("%s: HTTP %s — backing off", host, r.status_code)
            time.sleep(3 * (attempt + 1))
            if attempt == retries - 1:
                # Persistent wall: stop hammering this host for the rest of the
                # run, and make it visible rather than reporting "0 results".
                _blocked.add(host)
                log.error("%s: blocked after %d attempts", host, retries)
            continue

        if 500 <= r.status_code < 600:
            time.sleep(2 ** attempt)
            continue

        log.info("%s: HTTP %s", host, r.status_code)
        return None
    return None


def get_json(url: str, **kw):
    return get(url, json=True, **kw)


def blocked_hosts() -> list[str]:
    """Hosts that walled us off this run — surfaced in the run report."""
    return sorted(_blocked)
