"""VivaReal / ZAP Imóveis.

Both sites are served by the same backend ("glue-api"), which returns clean
JSON and is far steadier than their rendered HTML. One request shape covers
both portals — only the x-domain header changes.
"""

from __future__ import annotations

import logging

from .. import http
from ..models import Listing
from ..units import price_to_brl
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.vivareal")

NAME = "vivareal"
API = "https://glue-api.vivareal.com/v2/listings"

PORTALS = {
    "vivareal": ("www.vivareal.com.br", "https://www.vivareal.com.br"),
    "zap": ("www.zapimoveis.com.br", "https://www.zapimoveis.com.br"),
}

PAGE_SIZE = 100


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_paginas_por_fonte", 5))

    for portal, (domain, site) in PORTALS.items():
        for uf in criteria.states:
            state_name = UF_NAMES.get(uf, "").replace("-", " ").title()
            for page in range(max_pages):
                params = {
                    "addressState": state_name,
                    "business": "SALE",
                    "unitTypes": "RESIDENTIAL_ALLOTMENT_LAND,FARM",
                    "listingType": "USED",
                    "priceMin": int(criteria.price_min),
                    "priceMax": int(criteria.price_max),
                    "size": PAGE_SIZE,
                    "from": page * PAGE_SIZE,
                    "includeFields": (
                        "search(result(listings(listing(id,title,description,"
                        "pricingInfos,usableAreas,totalAreas,address),medias,link)))"
                    ),
                }
                data = http.get_json(
                    API, params=params,
                    headers={"x-domain": domain, "Origin": site, "Referer": site + "/"},
                )
                if not data:
                    break

                results = (
                    data.get("search", {}).get("result", {}).get("listings", [])
                )
                if not results:
                    break
                for entry in results:
                    listing = _to_listing(entry, site, uf)
                    if listing:
                        out.append(listing)
                if len(results) < PAGE_SIZE:
                    break
    log.info("vivareal/zap: %d listings", len(out))
    return out


def _to_listing(entry: dict, site: str, uf: str) -> Listing | None:
    node = entry.get("listing") or {}
    link = (entry.get("link") or {}).get("href", "")
    if not link:
        return None
    url = link if link.startswith("http") else site + link

    pricing = (node.get("pricingInfos") or [{}])[0]
    price = pricing.get("price") or pricing.get("salePrice")

    # These portals report land area in m², usually in totalAreas.
    areas = node.get("totalAreas") or node.get("usableAreas") or []
    area_m2 = None
    for value in areas:
        try:
            area_m2 = float(value)
            break
        except (TypeError, ValueError):
            continue

    address = node.get("address") or {}
    media = entry.get("medias") or []
    image = ""
    if media and isinstance(media[0], dict):
        image = (media[0].get("url") or "").replace("{action}", "fit-in").replace(
            "{width}", "400").replace("{height}", "300")

    return Listing(
        source=NAME,
        source_id=str(node.get("id") or ""),
        url=url,
        title=node.get("title") or "",
        description=node.get("description") or "",
        price=price_to_brl(str(price)) if price else None,
        area_ha=round(area_m2 / 10_000, 4) if area_m2 else None,
        municipality=address.get("city") or "",
        uf=address.get("stateAcronym") or uf,
        lat=_coord(address, "lat"),
        lon=_coord(address, "lon"),
        image=image,
    )


def _coord(address: dict, which: str):
    point = address.get("geoLocation", {}).get("location", {}) if address else {}
    value = point.get(which)
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None
