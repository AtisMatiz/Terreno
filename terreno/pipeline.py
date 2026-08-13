"""Normalize → dedup → filter → score. The deterministic middle of the run."""

from __future__ import annotations

import difflib
import logging
import re

from . import geo, scoring
from .config import Criteria, fold
from .extract.rules import extract as rules_extract
from .http import get as http_get
from .models import Listing
from .units import area_detalhada, preco_total, price_per_ha, price_to_brl

log = logging.getLogger("terreno.pipeline")


def normalize(listing: Listing) -> Listing:
    """Fill derived fields. Sources may set price/area directly or leave the
    raw text for this to parse — whichever is cheaper at the source."""
    text = f"{listing.title} {listing.description}"

    if listing.price is None:
        listing.price = price_to_brl(text)
    if listing.area_ha is None:
        # area_detalhada rather than area_to_ha: the alqueire is regionally
        # ambiguous (paulista 2.42 ha vs mineiro 4.84 -- a factor of two), and
        # when the UF does not settle which one the listing means, it returns
        # no hectare figure at all rather than a guess. Keeping the alqueire
        # count lets the card say "3 alqueires" honestly; a guessed hectare
        # figure would silently corrupt price-per-hectare and the area filter,
        # and would do it invisibly.
        detalhe = area_detalhada(text, listing.uf)
        listing.area_ha = detalhe.ha
        if detalhe.alqueires is not None:
            listing.area_alqueires = detalhe.alqueires
            listing.area_alqueire_tipo = detalhe.alqueire_tipo

    # "R$150.000 por alqueire" is a per-unit price, not the property's total
    # -- checked against the raw text regardless of whether `price` came
    # from here or from the source itself, since a source reading a
    # structured price field can still miss a per-unit qualifier that sits
    # in the free-text description.
    corrigido = preco_total(text, listing.area_ha, listing.area_alqueires)
    if corrigido is not None:
        listing.price = corrigido

    listing.uf = (listing.uf or "").upper()[:2]
    listing.municipality = (listing.municipality or "").strip()
    listing.price_per_ha = price_per_ha(listing.price, listing.area_ha)
    return listing


#  Minimum *normalized* description length before two listings are even
# candidates for the text-similarity pass below. Real found case (2026-08-13):
# two distinct Facebook listing_ids, genuinely different `key`s, no shared
# municipality (one was blank, so the price/area/municipality fuzzy match
# above never fired either) -- but an identical, specific paragraph
# ("Morro do Macuco... 18 km...") proving it was the same seller
# cross-posting the same plot twice. A short generic ad ("Terreno à venda,
# documentação regular") could coincidentally match another short generic ad
# that is a genuinely different property -- Brazilian listings are full of
# exactly that kind of boilerplate, which is why scoring.py's keyword system
# exists in the first place. Below this length, two ads are left alone.
_TEXTO_DUP_MIN_LEN = 120
_TEXTO_DUP_LIMIAR = 0.85   # similarity ratio, not a raw word-overlap percentage


def _normalizar_para_comparacao(text: str) -> str:
    """Case/accent/emoji/whitespace-insensitive text for the similarity pass."""
    folded = fold(text or "")
    return re.sub(r"[^a-z0-9 ]", "", folded)
    # (letters/digits/spaces only survive; emoji, punctuation, line breaks
    # all disappear, so two copies of the same ad compare equal regardless
    # of how each portal happened to render whitespace around them.)


def dedup(listings: list[Listing]) -> list[Listing]:
    """Collapse exact duplicates, then cross-source/cross-posting duplicates.

    When the same plot appears on two portals (or the same seller reposts it
    under a second listing id), the richer record wins — more description
    text means better scoring and a better card.
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

    # Third pass: catches the case the structured fuzzy key above cannot --
    # one side missing a municipality (or having a wildly different price
    # bucket typo) but both sides carrying the same specific, seller-written
    # paragraph. O(n²) over survivors only, and n is small (tens, not
    # thousands) at this point in the pipeline.
    kept: list[Listing] = []
    normalizados: list[str] = []
    for item in by_fuzzy.values():
        norm = _normalizar_para_comparacao(item.description)
        if len(norm) < _TEXTO_DUP_MIN_LEN:
            kept.append(item)
            normalizados.append(norm)
            continue
        indice_dup = next(
            (i for i, (outro, outro_norm) in enumerate(zip(kept, normalizados))
             if len(outro_norm) >= _TEXTO_DUP_MIN_LEN
             and difflib.SequenceMatcher(None, norm, outro_norm).ratio() >= _TEXTO_DUP_LIMIAR),
            None,
        )
        if indice_dup is None:
            kept.append(item)
            normalizados.append(norm)
        elif len(item.description) > len(kept[indice_dup].description):
            kept[indice_dup] = item
            normalizados[indice_dup] = norm
    return kept


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
                # Kept on the listing, not just tested and thrown away: the
                # scorer ranks by this distance, which is what makes a
                # neighbouring município outrank a far corner of the same
                # region instead of the two being indistinguishable.
                item.distancia_centro_km = round(
                    geo.haversine_km(center_coords, coords), 1)
                if item.distancia_centro_km > float(criteria.radius_km):
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
    nota_minima = criteria.nota_minima
    pph = criteria.raw.get("preco_por_ha") or {}
    bom = float(pph.get("ideal", scoring.PRECO_HA_BOM))
    limite = float(pph.get("teto_alerta", scoring.PRECO_HA_LIMITE))

    kept: list[Listing] = []
    descartados: dict[str, int] = {}
    for item in listings:
        # An auto-generated category title ("Estúdio 0 banheiros – Casa")
        # carries no real feature information -- see scoring.titulo_generico
        # -- and scoring it alongside the description inflates
        # "benfeitorias" with the category tag itself, not a real claim.
        titulo = "" if scoring.titulo_generico(item.title) else item.title
        text = f"{titulo} {item.description}"

        motivo = scoring.motivo_descarte(text, item.area_ha)
        if motivo:
            descartados[motivo] = descartados.get(motivo, 0) + 1
            continue

        # Price per hectare and distance to the centre of interest are scored
        # inside `avaliar` now, against the absolute thresholds from
        # criteria.yaml. They used to be a *relative* blend here -- 15% of the
        # score for being the cheapest R$/ha of whatever this particular run
        # happened to collect -- which measured the batch, not the property: on
        # a run of only expensive listings the dearest of them still scored
        # full marks. Absolute thresholds are what the owner actually asked
        # for, and keeping both would have double-counted price.
        nota, detalhe, evidencias, estrelas = scoring.avaliar(
            text,
            price_per_ha=item.price_per_ha,
            distancia_centro_km=item.distancia_centro_km,
            preco_ha_bom=bom,
            preco_ha_limite=limite,
            municipality=item.municipality,
            centro=criteria.center,
            zona_melhor=criteria.zona_melhor,
            zona_boa=criteria.zona_boa,
        )

        _, aviso = scoring.tipo_ok(text)
        item.score = nota
        item.dimensoes = detalhe
        item.reasons = ([aviso] if aviso else []) + evidencias
        item.estrelas = estrelas
        item.destaques = scoring.destaques(detalhe)

        # Above the ceiling the listing still belongs on the site -- it may be
        # worth it for reasons price alone does not capture -- but it does not
        # earn a Telegram ping. Discarding would lose it; notifying on
        # everything is the flood being fixed.
        item.notificavel = not (item.price_per_ha and item.price_per_ha > limite)

        if item.score >= nota_minima:
            kept.append(item)

    for motivo, n in sorted(descartados.items(), key=lambda kv: -kv[1]):
        log.info("%d anúncio(s) descartado(s): %s", n, motivo)
    kept.sort(key=lambda x: (x.score, x.first_seen), reverse=True)
    return kept


def enriquecer_imagens(listings: list[Listing], criteria: Criteria) -> int:
    """Lê a foto principal dos anúncios de nota alta. Devolve quantos leu.

    O corte mora aqui, e não dentro de `extract/imagem.py`, para que o gasto
    fique visível no lugar onde a decisão de gastar é tomada: palavra-chave é
    grátis e roda em todos os resultados, imagem custa por anúncio. Num run que
    descobre centenas de candidatos e aprova oito, a diferença é entre centenas
    de chamadas e oito.
    """
    corte = float(criteria.output("nota_minima_imagem", 70)) / 100.0
    alvos = [x for x in listings if x.score >= corte and x.image]
    if not alvos:
        return 0

    from .extract import imagem
    if imagem._client() is None:
        # Sem ENABLE_LLM/ANTHROPIC_API_KEY não há o que fazer, e dizer isso uma
        # vez é melhor que uma linha por anúncio.
        log.info("imagem: %d anúncio(s) acima de %.0f/100, mas a leitura de "
                 "imagem está desligada (precisa de ENABLE_LLM=1 e "
                 "ANTHROPIC_API_KEY)", len(alvos), corte * 100)
        return 0

    lidos = 0
    for item in alvos:
        analise = imagem.analisar(item.image)
        if not analise:
            continue
        item.imagem_analise = analise
        lidos += 1
        # A foto é evidência adicional, nunca substitui a nota determinística.
        # Uma imagem que nem mostra o imóvel (mapa, logotipo, foto de corretor)
        # é justamente o tipo de coisa que vale registrar.
        if analise.get("mostra_o_imovel") is False:
            item.reasons.append("⚠ foto não mostra o imóvel")
        elif analise.get("resumo"):
            item.reasons.append(f"foto: {analise['resumo']}")
    log.info("imagem: %d de %d anúncio(s) acima de %.0f/100 analisados",
             lidos, len(alvos), corte * 100)
    return lidos
