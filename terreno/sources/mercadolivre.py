"""Mercado Livre.

Their search API used to be open and now refuses anonymous callers outright,
so this source needs an app token. Measured 2026-08-11: the 403 it returns is
an *authentication* failure, not the datacenter-IP block it was long filed as
— a residential connection gets the identical 403, so an IP change alone will
never fix it.

Credentials come from a free app at https://developers.mercadolivre.com.br
(Devcenter → Criar aplicação). Two ways to supply them, in this order:

  * ML_CLIENT_ID + ML_CLIENT_SECRET — preferred, and the only one that keeps
    working unattended. The token is minted per run via the OAuth
    `client_credentials` grant, which needs no user interaction.
  * ML_ACCESS_TOKEN — a token pasted by hand. Kept as an escape hatch, but
    **Mercado Livre's tokens expire in about six hours**, so a pasted one
    stops working the same day and takes the source silently down with it.
    Prefer the pair above.

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
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
# MLB1495 = Imóveis > Terrenos e Fazendas
CATEGORY = "MLB1495"
PAGE_SIZE = 50

# Minted once per process. A run is a single process, so this is simply "once
# per run" -- no expiry tracking needed, since a ~6h token cannot age out
# inside one run, and persisting it across runs would buy one saved request at
# the cost of having to handle staleness.
_token_cache: str | None = None


def _token() -> str | None:
    """App token for the search API, or None if it cannot be obtained.

    Uses the `client_credentials` grant: it authenticates the *application*
    rather than a user, so there is no browser redirect and nothing to renew
    by hand -- which is what makes this source able to run unattended at all.
    """
    global _token_cache
    if _token_cache:
        return _token_cache

    manual = env("ML_ACCESS_TOKEN")
    if manual:
        log.info("mercadolivre: usando ML_ACCESS_TOKEN colado à mão — lembre que "
                 "ele expira em ~6h; ML_CLIENT_ID/ML_CLIENT_SECRET não expiram")
        _token_cache = manual
        return _token_cache

    client_id, client_secret = env("ML_CLIENT_ID"), env("ML_CLIENT_SECRET")
    if not (client_id and client_secret):
        log.info("mercadolivre: sem ML_CLIENT_ID/ML_CLIENT_SECRET (nem "
                 "ML_ACCESS_TOKEN) — a API recusa chamada anônima, então esta "
                 "fonte não tem como rodar. Crie um app grátis em "
                 "developers.mercadolivre.com.br")
        return None

    # POST, and terreno.http is GET-only by design -- same exception
    # facebook.py makes for the Apify actor call.
    import requests
    try:
        r = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials",
                  "client_id": client_id, "client_secret": client_secret},
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warning("mercadolivre: falha ao pedir token: %s", exc)
        return None

    if r.status_code != 200:
        # The body names the cause ("invalid_client" for a wrong secret,
        # "unsupported_grant_type" when the app has no Client Credentials
        # flow enabled) -- without it, every misconfiguration looks the same.
        log.error("mercadolivre: token recusado (HTTP %s) — %s",
                  r.status_code, (r.text or "")[:300].replace("\n", " "))
        return None

    try:
        dados = r.json()
    except ValueError:
        log.error("mercadolivre: resposta do token não era JSON")
        return None

    _token_cache = dados.get("access_token")
    if not _token_cache:
        log.error("mercadolivre: resposta do token sem access_token: %s",
                  str(dados)[:200])
        return None
    log.info("mercadolivre: token obtido via client_credentials (expira em %ss)",
             dados.get("expires_in", "?"))
    return _token_cache


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_paginas_por_fonte", 5))

    token = _token()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}

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
