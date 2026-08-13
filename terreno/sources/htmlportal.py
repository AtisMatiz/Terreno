"""Config-driven scraper for the portals that expose neither a JSON API nor a
Next.js payload — currently Chaves na Mão and Imovelweb.

Two design notes worth keeping:

* The *index* page's schema.org markup is useless here — these portals mark the
  index up as a Product, so reading it yields one fake listing per page
  ("17.023 Terrenos à venda" priced at R$ 17.023). Only detail pages are
  trustworthy.
* Chaves na Mão encodes area, price and id in the detail URL itself
  (`...-540m2-RS900000/id-42257793/`). Parsing the slug gives us the two fields
  the hard filters need for free, so only listings that survive filtering ever
  cost an HTTP request. That enrichment happens in pipeline.enrich().
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from .. import http
from ..extract import rules
from ..models import Listing
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.htmlportal")

PORTALS = {
    "chavesnamao": {
        "base": "https://www.chavesnamao.com.br",
        # Rural land is split across categories; "terrenos" alone returns urban
        # lots of a few hundred m², which the area filter then discards.
        "paths": [
            "/chacaras-a-venda/{uf_lower}/",
            "/fazendas-a-venda/{uf_lower}/",
            "/terrenos-a-venda/{uf_lower}/",
        ],
        "page_param": "pagina",
        # Slugs carry uppercase (RS900000), so this is deliberately not [a-z].
        "link_re": r"/imovel/[\w\-]+/id-\d+/",
        "slug": True,
    },
    # Wimoveis e Imovelweb são o mesmo motor (grupo Navent) — mesma estrutura
    # de URL e mesmo padrão de link, só muda o domínio.
    "wimoveis": {
        "base": "https://www.wimoveis.com.br",
        "paths": [
            "/chacaras-sitios-e-fazendas-venda-{uf_slug}.html",
            "/terrenos-venda-{uf_slug}.html",
        ],
        "page_param": "pagina",
        "link_re": r"/propriedades/[\w\-]+\.html",
        "slug": False,
    },
    "imovelweb": {
        "base": "https://www.imovelweb.com.br",
        "paths": [
            "/chacaras-sitios-e-fazendas-venda-{uf_slug}.html",
            "/terrenos-venda-{uf_slug}.html",
        ],
        "page_param": "pagina",
        "link_re": r"/propriedades/[\w\-]+\.html",
        "slug": False,
    },
}

# Detail pages fetched per portal per run when a slug cannot be parsed.
DEFAULT_DETAIL_CAP = 60

_SLUG_RE = re.compile(
    r"/imovel/(?P<slug>[\w\-]+?)-(?P<area>\d+)m2-RS(?P<price>\d+)/id-(?P<id>\d+)/",
    re.I,
)


def make_fetcher(name: str):
    """Build the standard fetch(criteria, store, budgets) callable for a portal."""
    spec = PORTALS[name]

    def fetch(criteria, store, budgets) -> list[Listing]:
        max_pages = int(budgets.get("max_paginas_por_fonte", 5))
        detail_cap = int(budgets.get("max_paginas_detalhe_por_fonte", DEFAULT_DETAIL_CAP))

        candidates: dict[str, str] = {}   # url -> uf
        for uf in criteria.states:
            for template in spec["paths"]:
                path = template.format(
                    uf_lower=uf.lower(), uf_slug=UF_NAMES.get(uf, uf.lower())
                )
                for page in range(1, max_pages + 1):
                    url = urljoin(spec["base"], path)
                    resp = http.get(
                        url, params={spec["page_param"]: page} if page > 1 else None
                    )
                    if resp is None:
                        break
                    found = _harvest(resp.text, spec)
                    for u in found:
                        candidates.setdefault(u, uf)
                    if not found:
                        break

        if spec["slug"]:
            out = [_from_slug(url, uf, name) for url, uf in candidates.items()]
            out = [x for x in out if x]
            log.info("%s: %d listings from %d links (no detail fetch yet)",
                     name, len(out), len(candidates))
            return out

        # No parseable slug: fall back to fetching detail pages directly,
        # skipping anything already stored. Only queried here (never for the
        # slug-based branch above, which never reads it) -- a full `SELECT url
        # FROM listings` scan for every run is wasted work for a portal that
        # has no use for the result.
        already = store.seen_urls()
        fresh = [(u, uf) for u, uf in candidates.items() if u not in already]
        log.info("%s: %d links (%d new, fetching up to %d)",
                 name, len(candidates), len(fresh), detail_cap)
        out = []
        for url, uf in fresh[:detail_cap]:
            resp = http.get(url, retries=2)
            if resp is None:
                continue
            listing = rules.extract(resp.text, url, source=name)
            if listing:
                listing.uf = listing.uf or uf
                out.append(listing)
        log.info("%s: %d listings", name, len(out))
        return out

    return fetch


def _from_slug(url: str, uf: str, name: str) -> Listing | None:
    """Build a listing from the URL alone. Municipality and description stay
    empty — pipeline.enrich() fills them in for the ones that pass filtering."""
    m = _SLUG_RE.search(url)
    if not m:
        return None
    words = m.group("slug").split("-")
    # "terreno-a-venda-mg-belo-horizonte-cinquentenario" — the UF sits right
    # after "venda"; everything after it is city + neighbourhood, which cannot
    # be split reliably, so the title keeps it readable and enrich() resolves
    # the actual municipality.
    slug_uf = ""
    for i, word in enumerate(words):
        if len(word) == 2 and word.upper() in UF_NAMES and i > 0:
            slug_uf = word.upper()
            break

    # "0m2" is the portal's way of saying the area was not filled in — treat it
    # as unknown, not as a zero-hectare plot that the area filter would drop.
    area_m2 = int(m.group("area"))
    return Listing(
        source=name,
        source_id=m.group("id"),
        url=url,
        title=" ".join(words).replace("a venda", "à venda").strip().capitalize(),
        price=float(m.group("price")) or None,
        area_ha=round(area_m2 / 10_000, 4) if area_m2 > 0 else None,
        uf=slug_uf or uf,
    )


def _harvest(html: str, spec: dict) -> list[str]:
    """Detail-page URLs on an index page, in document order, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(spec["link_re"], html):
        full = urljoin(spec["base"], m.group(0))
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


fetch_chavesnamao = make_fetcher("chavesnamao")
fetch_wimoveis = make_fetcher("wimoveis")
fetch_imovelweb = make_fetcher("imovelweb")
