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


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))
