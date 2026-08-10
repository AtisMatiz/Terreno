"""OLX — the largest classifieds source for rural land in Brazil.

OLX is a Next.js app, so we read __NEXT_DATA__ rather than the rendered cards.
It also sits behind aggressive bot protection; a 403 here is normal and is
reported as a blocked host rather than as "no listings found".
"""

from __future__ import annotations

import logging

from .. import http
from ..models import Listing
from ..units import area_to_ha, price_to_brl
from .base import UF_NAMES, next_data, walk

log = logging.getLogger("terreno.sources.olx")

NAME = "olx"
BASE = "https://www.olx.com.br/imoveis/terrenos"


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_pages_per_source", 5))

    for uf in criteria.states:
        slug = UF_NAMES.get(uf)
        if not slug:
            continue
        for page in range(1, max_pages + 1):
            url = f"{BASE}/estado-{uf.lower()}"
            params = {"o": page, "ps": int(criteria.price_min) or None,
                      "pe": int(criteria.price_max)}
            params = {k: v for k, v in params.items() if v}
            resp = http.get(url, params=params)
            if resp is None:
                break

            data = next_data(resp.text)
            if not data:
                log.warning("olx: no __NEXT_DATA__ on %s p%d", uf, page)
                break

            ads = _extract_ads(data)
            if not ads:
                break
            for ad in ads:
                listing = _to_listing(ad, uf)
                if listing:
                    out.append(listing)
            if len(ads) < 20:
                break  # last page
    log.info("olx: %d listings", len(out))
    return out


def _extract_ads(data: dict) -> list[dict]:
    """OLX has moved this payload around between releases; take whichever
    container is present rather than pinning one path."""
    for key in ("ads", "listings", "adList"):
        for value in walk(data, key):
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _to_listing(ad: dict, uf: str) -> Listing | None:
    url = ad.get("url") or ad.get("friendlyUrl") or ""
    if not url:
        return None
    if url.startswith("/"):
        url = "https://www.olx.com.br" + url

    props = {}
    for prop in ad.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name"):
            props[prop["name"]] = prop.get("value")

    area = props.get("size") or props.get("area") or ""
    price = ad.get("price") or props.get("price") or ""

    return Listing(
        source=NAME,
        source_id=str(ad.get("listId") or ad.get("id") or ""),
        url=url,
        title=ad.get("subject") or ad.get("title") or "",
        description=ad.get("body") or ad.get("description") or "",
        price=price_to_brl(price),
        area_ha=area_to_ha(f"{area} m2" if str(area).isdigit() else str(area), uf),
        municipality=(ad.get("location") or {}).get("municipality", "")
        if isinstance(ad.get("location"), dict) else ad.get("locationDetails", ""),
        uf=uf,
        image=(ad.get("thumbnail") or ""),
        posted_at=str(ad.get("date") or ad.get("listTime") or ""),
    )
