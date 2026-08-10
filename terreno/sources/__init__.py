"""Source registry. Adding a portal means adding one line here."""

from __future__ import annotations

from . import brave, facebook, htmlportal, mercadolivre, olx, vivareal

# name -> fetch(criteria, store, budgets) -> list[Listing]
REGISTRY = {
    "mercadolivre": mercadolivre.fetch,   # cheapest and most reliable; runs first
    "vivareal": vivareal.fetch,
    "olx": olx.fetch,
    "chavesnamao": htmlportal.fetch_chavesnamao,
    "imovelweb": htmlportal.fetch_imovelweb,
    "brave": brave.fetch,
    "facebook": facebook.fetch,
}
