"""Normalize → dedup → filter → score. The deterministic middle of the run."""

from __future__ import annotations

import logging

from . import geo, scoring
from .config import Criteria, fold
from .extract.rules import extract as rules_extract
from .http import get as http_get
from .models import Listing
from .units import area_to_ha, price_per_ha, price_to_brl

log = logging.getLogger("terreno.pipeline")


def normalize(listing: Listing) -> Listing:
    """Fill derived fields. Sources may set price/area directly or leave the
    raw text for this to parse — whichever is cheaper at the source."""
    text = f"{listing.title} {listing.description}"

    if listing.price is None:
        listing.price = price_to_brl(text)
    if listing.area_ha is None:
        listing.area_ha = area_to_ha(text, listing.uf)

    listing.uf = (listing.uf or "").upper()[:2]
    listing.municipality = (listing.municipality or "").strip()
    listing.price_per_ha = price_per_ha(listing.price, listing.area_ha)
    return listing


def dedup(listings: list[Listing]) -> list[Listing]:
    """Collapse exact duplicates, then cross-source duplicates.

    When the same plot appears on two portals, the richer record wins — more
    description text means better scoring and a better card.
    """
    by_key: dict[str, Listing] = {}
    for item in listings:
        existing = by_key.get(item.key)
        if existing is None or len(item.description) > len(existing.description):
            by_key[item.key] = item

    by_fuzzy: dict[str, Listing] = {}
    for item in by_key.values():
        # Only cross-match when there is enough signal to be confident; a
        # listing with no price or no area would collapse everything together.
        if not (item.price and item.area_ha and item.municipality):
            by_fuzzy[item.key] = item
            continue
        fk = item.fuzzy_key
        existing = by_fuzzy.get(fk)
        if existing is None or len(item.description) > len(existing.description):
            by_fuzzy[fk] = item
    return list(by_fuzzy.values())


def apply_filters(listings: list[Listing], criteria: Criteria, store) -> list[Listing]:
    """Hard filters. Unknown values pass rather than fail.

    A listing with an unparseable price is shown, not dropped — a missing field
    is a parsing gap on our side, and silently hiding it would be the single
    easiest way for this tool to miss the plot you actually wanted.
    """
    center_coords = None
    if criteria.center and criteria.radius_km:
        center_coords = geo.geocode(criteria.center, store)
        if not center_coords:
            log.warning("center %r could not be geocoded — radius filter off",
                        criteria.center)

    kept: list[Listing] = []
    for item in listings:
        if item.price is not None and not (criteria.price_min <= item.price <= criteria.price_max):
            continue
        if item.area_ha is not None and not (criteria.area_min <= item.area_ha <= criteria.area_max):
            continue
        if (criteria.max_price_per_ha and item.price_per_ha
                and item.price_per_ha > criteria.max_price_per_ha):
            continue
        if criteria.states and item.uf and item.uf not in criteria.states:
            continue
        if criteria.municipalities and item.municipality:
            # Accent-insensitive: region lists are written properly
            # ("São Bento do Sapucaí") while listings often are not.
            wanted = {fold(m) for m in criteria.municipalities}
            if fold(item.municipality) not in wanted:
                continue

        if center_coords and item.municipality and item.uf:
            coords = geo.geocode(f"{item.municipality}, {item.uf}", store)
            if coords:
                item.lat, item.lon = coords
                if geo.haversine_km(center_coords, coords) > float(criteria.radius_km):
                    continue
        kept.append(item)
    return kept


def enrich(listings: list[Listing], budgets: dict) -> list[Listing]:
    """Fetch detail pages for listings that arrived without a description.

    Deliberately placed *after* filtering: sources that can read price and area
    from a URL slug hand us thousands of cheap stubs, and only the handful that
    survive the hard filters is worth an HTTP request. Everything scoring needs
    lives in the description, so this is what makes the free scorer work.
    """
    cap = int(budgets.get("max_paginas_enriquecimento", 80))
    pending = [x for x in listings if not x.description]
    if not pending:
        return listings

    log.info("enriching %d of %d listings (cap %d)", len(pending), len(listings), cap)
    for item in pending[:cap]:
        resp = http_get(item.url)
        if resp is None:
            continue
        detail = rules_extract(resp.text, item.url, source=item.source)
        if not detail:
            continue
        item.description = detail.description or item.description
        # The detail page's own title beats one reconstructed from a URL slug.
        item.title = detail.title or item.title
        item.municipality = item.municipality or detail.municipality
        item.uf = item.uf or detail.uf
        item.image = item.image or detail.image
        if item.price is None:
            item.price = detail.price
        if item.area_ha is None:
            item.area_ha = detail.area_ha
        item.price_per_ha = price_per_ha(item.price, item.area_ha)
    return listings


def score_all(listings: list[Listing], criteria: Criteria) -> list[Listing]:
    """Weighted scoring against the fixed buyer profile (terreno/scoring.py).

    Two things happen here that a flat keyword list could not do: the rural
    gate drops urban lots and town houses outright, and each dimension keeps
    its own sub-score so the page can explain the ranking.
    """
    ppha = [x.price_per_ha for x in listings if x.price_per_ha]
    melhor = min(ppha) if ppha else None
    nota_minima = criteria.nota_minima

    kept: list[Listing] = []
    descartados = 0
    for item in listings:
        text = f"{item.title} {item.description}"

        rural, aviso = scoring.tipo_ok(text)
        if not rural:
            descartados += 1
            continue

        nota, detalhe, evidencias = scoring.avaliar(text)

        valor = 0.0
        if melhor and item.price_per_ha:
            # 1.0 at the cheapest R$/ha this run, decaying as it gets dearer.
            valor = max(0.0, min(1.0, melhor / item.price_per_ha))
            evidencias.append(f"R$/ha {item.price_per_ha:,.0f}".replace(",", "."))

        # Price is a constraint, not the point — the profile dominates.
        item.score = round(0.85 * nota + 0.15 * valor, 3)
        item.dimensoes = detalhe
        item.reasons = ([aviso] if aviso else []) + evidencias

        if item.score >= nota_minima:
            kept.append(item)

    if descartados:
        log.info("%d anúncios descartados por não serem imóvel rural", descartados)
    kept.sort(key=lambda x: (x.score, x.first_seen), reverse=True)
    return kept
