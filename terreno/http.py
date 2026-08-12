"""Shared HTTP client. Every source goes through this — polite rate limiting,
retries, and a per-host backoff live here once rather than in five scrapers.
"""

from __future__ import annotations

import logging
import os
import random
import threading
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

# Layer B now visits candidate pages from several threads at once, so the
# per-host throttle has to be safe under concurrency, not just correct for a
# single caller. A lock guards only the bookkeeping (reserving the next
# allowed slot for a host), never the sleep itself -- holding it during
# time.sleep() would serialize every request onto one host's clock, exactly
# the throughput parallelism is meant to buy back.
_throttle_lock = threading.Lock()
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
# Deliberately catches more than ImportError: curl_cffi ships a compiled
# extension, and when that fails to load (wrong architecture, missing system
# library, half-finished install) the exception is an OSError, not an
# ImportError. Treating only ImportError as "absent" would report a broken
# install as a missing one -- two problems whose fixes have nothing in common.
# The reason is kept so it can be shown instead of a bare "not installed".
try:
    from curl_cffi import requests as _cffi
    _cffi_erro = ""
except Exception as _exc:  # noqa: BLE001 — see above
    _cffi = None
    _cffi_erro = f"{type(_exc).__name__}: {_exc}"

# Which browser curl_cffi imitates. Overridable without a code change because
# anti-bot vendors ship fingerprint databases at their own pace: when one
# target stops working, the next one along often still does, and finding that
# out is a one-command experiment (TERRENO_IMPERSONATE=chrome131 ...) rather
# than an edit-commit-rerun cycle.
IMPERSONATE = os.getenv("TERRENO_IMPERSONATE", "chrome")

_cffi_ok: set[str] = set()      # hosts the browser transport got through
_cffi_falhou: set[str] = set()  # ...and hosts where it was tried and did not

# Headers that describe *who is asking* rather than *what is being asked for*.
# curl_cffi's `impersonate` sets a complete, self-consistent set of these to
# match the TLS and HTTP/2 handshake it performs. Overriding any of them
# re-creates exactly the mismatch the impersonation exists to remove: a
# "Chrome 124 on Windows" User-Agent arriving over a current-Chrome handshake
# is not a neutral detail, it is itself a bot signal to Cloudflare/DataDome,
# which cross-check the two. Passing DEFAULT_HEADERS into curl_cffi -- which
# this module did until now -- therefore sabotaged the very fallback it was
# calling, and did it silently.
_HEADERS_DE_IMPRESSAO = {
    "user-agent", "accept", "accept-language", "accept-encoding",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
}


def _headers_semanticos(headers: dict | None) -> dict:
    """Só os cabeçalhos que dizem o que estamos pedindo (x-domain, Origin,
    Referer, tokens de API) -- nunca os que dizem quem somos."""
    return {k: v for k, v in (headers or {}).items()
            if k.lower() not in _HEADERS_DE_IMPRESSAO}


def _throttle(host: str) -> None:
    """Reserve this thread's slot for `host`, then sleep outside the lock.

    Two threads hitting *different* hosts never wait on each other at all --
    each reservation only depends on that host's own last slot. Two threads
    hitting the *same* host get staggered by the reservation itself, so the
    politeness guarantee holds even under concurrency.
    """
    interval = HOST_INTERVAL.get(host, MIN_INTERVAL) + random.uniform(0, 0.4)
    with _throttle_lock:
        now = time.monotonic()
        proxima_vez = max(now, _last_hit[host] + interval)
        _last_hit[host] = proxima_vez
    espera = proxima_vez - time.monotonic()
    if espera > 0:
        time.sleep(espera)


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
        # A host where the browser transport already proved necessary goes
        # straight to it: repeating a plain attempt we have measured to fail
        # only spends another throttle slot to learn the same thing again.
        if host in _cffi_ok:
            _throttle(host)
            pronto = _via_cffi(url, params, headers, timeout, json)
            if pronto is not None:
                return pronto

        _throttle(host)
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("%s: %s (attempt %d)", host, exc, attempt + 1)
            # A connection torn down at the TLS layer (SSLZeroReturnError,
            # connection reset) is a *rejection*, not an outage -- and it is
            # precisely the shape a fingerprint block takes when the wall sits
            # in front of the server rather than in it. Until now curl_cffi was
            # only ever reached from the 403/429 branch below, so this whole
            # class of block -- PGFN's, measured repeatedly -- was recorded as
            # "curl_cffi doesn't help here" without curl_cffi ever being tried.
            alt = _tentar_cffi(url, params, headers, timeout, json, host,
                               motivo=f" (após {type(exc).__name__})")
            if alt is not None:
                return alt
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
            # Try the browser-fingerprint transport before backing off.
            alt = _tentar_cffi(url, params, headers, timeout, json, host,
                               motivo=f" (após HTTP {r.status_code})")
            if alt is not None:
                return alt
            # The body goes in the log for the same reason the generic-4xx
            # branch below does it: "token inválido", "scope insuficiente" and
            # "your IP is blocked" all arrive as a bare 403, and without the
            # body they are indistinguishable -- which is precisely the
            # confusion that had Mercado Livre's auth failure filed as an IP
            # block for weeks.
            trecho = (r.text or "")[:200].replace("\n", " ")
            log.warning("%s: HTTP %s — backing off%s", host, r.status_code,
                        f" — {trecho}" if trecho else "")
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

        # A 4xx here means the request itself was rejected — a bad or
        # unsupported parameter, not a wall to back off from. The body usually
        # says exactly what was wrong (Brave's 422s do), and without it a bad
        # parameter looks identical to a dead source until someone downloads
        # the workflow log and greps for it by hand.
        trecho = (r.text or "")[:400].replace("\n", " ")
        log.warning("%s: HTTP %s — %s", host, r.status_code, trecho)
        return None
    return None


def _tentar_cffi(url, params, headers, timeout, want_json, host, motivo=""):
    """Uma tentativa com o transporte de navegador por host por execução,
    dizendo no log o que aconteceu.

    O silêncio era o problema real da versão anterior: a falha do curl_cffi
    só aparecia em DEBUG, então uma execução normal não distinguia "o
    curl_cffi foi tentado e não passou" de "o curl_cffi nem existe aqui" --
    e as duas pedem providências opostas.
    """
    if _cffi is None:
        if host not in _cffi_falhou:
            _cffi_falhou.add(host)
            log.warning("%s: bloqueado e o curl_cffi não pôde ser carregado "
                        "(%s) — `python3 -m pip install curl_cffi` (com o -m, "
                        "para instalar no mesmo Python que roda isto aqui) "
                        "habilita a tentativa com impressão digital de navegador",
                        host, _cffi_erro or "não instalado")
        return None
    if host in _cffi_falhou:
        return None

    resultado = _via_cffi(url, params, headers, timeout, want_json, motivo=motivo)
    if resultado is None:
        _cffi_falhou.add(host)
    else:
        _cffi_ok.add(host)
        log.info("%s: liberado via curl_cffi (impersonate=%s)%s",
                 host, IMPERSONATE, motivo)
    return resultado


def _via_cffi(url, params, headers, timeout, want_json, *, motivo=""):
    """One attempt with a browser TLS fingerprint. Never raises.

    Deliberately does not forward DEFAULT_HEADERS -- see
    `_HEADERS_DE_IMPRESSAO` for why sending our own User-Agent here defeats
    the impersonation instead of helping it.
    """
    if _cffi is None:
        return None
    host = urlsplit(url).netloc
    try:
        r = _cffi.get(
            url, params=params,
            headers=_headers_semanticos(headers),
            timeout=timeout, impersonate=IMPERSONATE,
        )
    except Exception as exc:  # noqa: BLE001 — optional path, must not break a run
        log.warning("%s: curl_cffi (impersonate=%s) falhou%s: %s: %s",
                    host, IMPERSONATE, motivo, type(exc).__name__, exc)
        return None
    if r.status_code != 200:
        log.warning("%s: curl_cffi (impersonate=%s) devolveu HTTP %s%s",
                    host, IMPERSONATE, r.status_code, motivo)
        return None
    if want_json:
        try:
            return r.json()
        except ValueError:
            log.warning("%s: curl_cffi passou mas a resposta não era JSON", host)
            return None
    return r


def get_json(url: str, **kw):
    return get(url, json=True, **kw)


def blocked_hosts() -> list[str]:
    """Hosts that walled us off this run — surfaced in the run report."""
    return sorted(_blocked)
