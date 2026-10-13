"""Geocoding without an API key.

Municipality names are resolved lazily through Nominatim and cached forever in
SQLite, so the cost of the radius filter is a handful of requests on the first
run and zero thereafter. There is no geocoding API key anywhere in this project.
"""

from __future__ import annotations

import logging
import math
import time

from . import http

log = logging.getLogger("terreno.geo")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy: max 1 req/s and a real identifying User-Agent.
_HEADERS = {"User-Agent": "terreno-land-search/1.0 (github.com/AtisMatiz/Terreno)"}
_last_call = 0.0


def geocode(query: str, store) -> tuple[float, float] | None:
    """Resolve 'Município, UF' to coordinates, cached in the store."""
    global _last_call
    if not query:
        return None

    cached = store.geocode_cached(query)
    if cached:
        return cached if cached[0] is not None else None

    elapsed = time.monotonic() - _last_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_call = time.monotonic()

    data = http.get_json(
        NOMINATIM,
        params={"q": f"{query}, Brasil", "format": "json", "limit": 1},
        headers=_HEADERS,
        retries=2,
    )
    if not data:
        # Cache the miss too, so a name that Nominatim cannot resolve is not
        # retried on every single run forever.
        store.geocode_put(query, None, None)
        return None

    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    store.geocode_put(query, lat, lon)
    return lat, lon


NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"


def reverse(lat: float, lon: float, store) -> tuple[str, str] | None:
    """(município, UF) of a coordinate, cached in the store.

    Marketplace often gives a listing's latitude/longitude but no readable
    city (all 49 sale listings of one real run had a blank município), and a
    blank município silently bypasses the region filter. Coordinates are
    rounded to ~1 km for the cache key: it is a town we want, not an address.
    A miss is not cached (a transient failure must not blank a place forever)."""
    global _last_call
    chave = f"{lat:.2f},{lon:.2f}"
    cached = store.reverse_cached(chave)
    if cached:
        return cached

    elapsed = time.monotonic() - _last_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_call = time.monotonic()

    data = http.get_json(
        NOMINATIM_REVERSE,
        params={"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "json",
                "zoom": 10, "addressdetails": 1, "accept-language": "pt-BR"},
        headers=_HEADERS, retries=2,
    )
    addr = (data or {}).get("address") or {}
    muni = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or "")
    uf = str(addr.get("ISO3166-2-lvl4") or "").replace("BR-", "")[:2].upper()
    if not muni:
        return None
    store.reverse_put(chave, muni, uf)
    return muni, uf


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))
