"""Source registry. Adding a portal means adding one line here."""

from __future__ import annotations

from . import (brave, caixa, facebook, htmlportal, imobiliaria_crawl,
               mercadolivre, olx, pgfn, vivareal)

# name -> fetch(criteria, store, budgets) -> list[Listing]
REGISTRY = {
    "mercadolivre": mercadolivre.fetch,   # cheapest and most reliable; runs first
    "pgfn": pgfn.fetch,                   # leilões da PGFN, API pública
    "vivareal": vivareal.fetch,
    "olx": olx.fetch,
    "chavesnamao": htmlportal.fetch_chavesnamao,
    "imovelweb": htmlportal.fetch_imovelweb,
    "wimoveis": htmlportal.fetch_wimoveis,
    "caixa": caixa.fetch,                 # CSV público, roda em CI
    # Nome trocado de "brave" para "sdb_scan" (2026-08-17): a rotação diária
    # da SDB agora é toda via Tavily (ver terreno/sources/tavily_discover.py),
    # não Brave -- manter o nome antigo aqui só produzia um alerta de saúde
    # confuso ("brave: sem resultado...") para algo que já não usa Brave.
    # Listings individuais continuam com source="brave" (brave_visit.py) --
    # esse é o atributo por-anúncio, sem relação com este nome de fonte.
    "sdb_scan": brave.fetch,
    "brave_novos": brave.fetch_novos,     # descoberta genérica de sites, roda como job separado
    "imobiliaria_crawl": imobiliaria_crawl.fetch,  # varredura sem API, roda 2x/semana
    "facebook": facebook.fetch,
}
