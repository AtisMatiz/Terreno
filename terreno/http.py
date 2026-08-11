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

# Hosts that need more room. comprei.pgfn.gov.br resets the connection under
# even a modest burst, so it gets a much slower cadence than the portals.
HOST_INTERVAL = {
    "comprei.pgfn.gov.br": 6.0,
    "nominatim.openstreetmap.org": 1.1,
}

_last_hit: dict[str, float] = defaultdict(float)
_blocked: set[str] = set()

_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)

# Optional second transport. Several Brazilian portals (Caixa/Radware, OLX,
# Imovelweb/Wimoveis) fingerprint the TLS handshake rather than the headers, so
# they answer 403 to `requests` while letting a real browser through. curl_cffi
# reproduces a browser's handshake and often clears exactly those walls.
#
# It is optional on purpose: `pip install curl_cffi` enables it, its absence
# changes nothing. It was NOT verifiable during development — the sandbox this
# was written in routes through a TLS-terminating proxy, which replaces any
# fingerprint we send, so whether it clears a given portal has to be measured
# where the code actually runs.
try:
    from curl_cffi import requests as _cffi
except ImportError:
    _cffi = None

_cffi_hosts: set[str] = set()   # hosts where plain requests hit a wall


def _throttle(host: str) -> None:
    elapsed = time.monotonic() - _last_hit[host]
    wait = HOST_INTERVAL.get(host, MIN_INTERVAL) - elapsed
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
            # Try the browser-fingerprint transport once before backing off.
            if _cffi is not None and host not in _cffi_hosts:
                _cffi_hosts.add(host)
                alt = _via_cffi(url, params, headers, timeout, json)
                if alt is not None:
                    log.info("%s: liberado via curl_cffi", host)
                    return alt
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


def _via_cffi(url, params, headers, timeout, want_json):
    """One attempt with a browser TLS fingerprint. Never raises."""
    try:
        r = _cffi.get(
            url, params=params,
            headers={**DEFAULT_HEADERS, **(headers or {})},
            timeout=timeout, impersonate="chrome",
        )
    except Exception as exc:  # noqa: BLE001 — optional path, must not break a run
        log.debug("curl_cffi falhou em %s: %s", url, exc)
        return None
    if r.status_code != 200:
        return None
    if want_json:
        try:
            return r.json()
        except ValueError:
            return None
    return r


def get_json(url: str, **kw):
    return get(url, json=True, **kw)


def blocked_hosts() -> list[str]:
    """Hosts that walled us off this run — surfaced in the run report."""
    return sorted(_blocked)
