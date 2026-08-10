"""Mercado Livre.

Their search API used to be open; it now answers 403 to anonymous callers and
to datacenter IPs. Two things make it work again, either of which is enough:

  * ML_ACCESS_TOKEN — create a free app at https://developers.mercadolivre.com.br
    and paste the token. This is the reliable path and works from CI.
  * running from a residential IP (the `local` profile).

Without either, this source logs the refusal and returns nothing rather than
pretending there are no listings.
"""

from __future__ import annotations

import logging

from .. import http
from ..config import env
from ..models import Listing
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.mercadolivre")

NAME = "mercadolivre"
API = "https://api.mercadolibre.com/sites/MLB/search"
# MLB1495 = Imóveis > Terrenos e Fazendas
CATEGORY = "MLB1495"
PAGE_SIZE = 50


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_paginas_por_fonte", 5))

    token = env("ML_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    if not token:
        log.info("ML_ACCESS_TOKEN not set — anonymous access is usually refused")

    for uf in criteria.states:
        state_name = UF_NAMES.get(uf, "").replace("-", " ")
        for page in range(max_pages):
            params = {
                "category": CATEGORY,
                "q": f"terreno chacara fazenda {state_name}",
                "price": f"{int(criteria.price_min)}-{int(criteria.price_max)}",
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
            }
            data = http.get_json(API, params=params, headers=headers)
            if not data:
                break

            results = data.get("results") or []
            if not results:
                break
            for item in results:
                listing = _to_listing(item, uf)
                if listing:
                    out.append(listing)
            if len(results) < PAGE_SIZE:
                break
    log.info("mercadolivre: %d listings", len(out))
    return out


def _to_listing(item: dict, uf: str) -> Listing | None:
    url = item.get("permalink")
    if not url:
        return None

    area_ha = None
    for attr in item.get("attributes") or []:
        if attr.get("id") in ("TOTAL_AREA", "LAND_AREA", "COVERED_AREA"):
            value = (attr.get("value_struct") or {})
            number, unit = value.get("number"), (value.get("unit") or "").lower()
            if number:
                if unit in ("ha", "hectare", "hectares"):
                    area_ha = float(number)
                elif unit in ("m²", "m2", "m"):
                    area_ha = float(number) / 10_000
                if area_ha:
                    break

    address = item.get("address") or {}
    return Listing(
        source=NAME,
        source_id=str(item.get("id") or ""),
        url=url,
        title=item.get("title") or "",
        description="",  # search results omit it; the title carries the signal
        price=float(item["price"]) if item.get("price") else None,
        area_ha=area_ha,
        municipality=address.get("city_name") or "",
        uf=(address.get("state_name") or uf)[:2].upper()
        if len(address.get("state_name") or "") == 2 else uf,
        image=(item.get("thumbnail") or "").replace("-I.jpg", "-O.jpg"),
    )
