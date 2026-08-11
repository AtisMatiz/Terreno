"""Layer B — long-tail discovery via the Brave Search API.

The portals in Layer A cover the big marketplaces. This covers everything else:
regional broker sites, town classifieds, public Facebook posts that Google and
Brave have indexed, and the one-page listings that never reach a portal.

Two guards matter here and are both enforced against the SQLite ledger:
  * a per-run query cap, and
  * a per-month cap, so a daily schedule cannot burn the free tier by the 20th.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timezone

from .. import http
from ..config import llm_enabled
from ..extract import rules
from ..models import Listing
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.brave")

NAME = "brave"
API = "https://api.search.brave.com/res/v1/web/search"
RESOURCE = "brave_queries"

# Query templates, aimed at the profile actually being looked for: rural land
# with a homestead and water, not urban lots. Each is expanded per place.
TEMPLATES = [
    'fazenda à venda {place} nascente',
    'chácara à venda {place} nascente',
    'sítio à venda {place} nascente',
    'sítio à venda {place} casa sede',
    'chácara à venda {place} casa hectares',
    'fazenda à venda {place} hectares água',
    '"vendo sítio" {place}',
    '"vendo chácara" OR "vendo fazenda" {place}',
    'sítio {place} sossegado mata nativa',
    'chácara {place} rio riacho hectares venda',
    'sítio OR chácara {place} site:facebook.com',
    'fazenda OR sítio {place} escriturada hectares venda',
]

# Hosts already covered by Layer A — no point spending a page fetch on them.
SKIP_HOSTS = (
    "olx.com.br", "vivareal.com.br", "zapimoveis.com.br",
    "mercadolivre.com.br", "chavesnamao.com.br", "imovelweb.com.br",
)


def _daily_allowance(store, per_run: int, per_month: int) -> int:
    """Spend up to `per_run`, but taper as the month's headroom shrinks so the
    free tier lasts to the last day instead of dying mid-month."""
    now = datetime.now(timezone.utc)
    days_total = monthrange(now.year, now.month)[1]
    days_left = max(1, days_total - now.day + 1)
    remaining = store.budget_remaining(RESOURCE, per_month)
    fair_share = int(remaining // days_left)
    return max(0, min(per_run, max(fair_share, 0)))


def _site_queries(criteria) -> list[str]:
    """One `site:` query per target domain, aimed at the region (or state).

    This is how the search covers dozens of portals and agencies without a
    scraper for each. Brave is an API with a key, so these reach sites that
    refuse our datacenter IP directly — including wimoveis and imovelweb, which
    answer 403 to the scrapers but are perfectly readable through search.
    """
    sites = criteria.raw.get("sites_alvo") or []
    if not sites:
        return []
    uf = criteria.states[0] if criteria.states else ""
    alvo = criteria.regiao or (UF_NAMES.get(uf, uf).replace("-", " ") if uf else "")
    if not alvo:
        return []
    return [f"fazenda OR sítio OR chácara à venda {alvo} site:{d}" for d in sites]


def _places(criteria) -> list[str]:
    """Places to search, most specific first.

    Municipalities (explicit or from the named region) are worth far more than
    a whole state here, so they lead; the region name and state come after as
    catch-alls, and the per-run budget truncates the tail.
    """
    uf = criteria.states[0] if criteria.states else ""
    places = [f"{m} {uf}".strip() for m in criteria.municipalities]
    if criteria.regiao:
        places.append(f"{criteria.regiao} {uf}".strip())
    places.extend(UF_NAMES.get(u, u).replace("-", " ") for u in criteria.states)
    return places


def fetch(criteria, store, budgets) -> list[Listing]:
    from ..config import env
    token = env("BRAVE_API_KEY")
    if not token:
        log.warning("BRAVE_API_KEY not set — Layer B skipped")
        return []

    allowance = _daily_allowance(
        store,
        int(budgets.get("brave_consultas_por_run", 50)),
        int(budgets.get("brave_consultas_por_mes", 2000)),
    )
    if allowance <= 0:
        log.warning("brave: monthly query budget exhausted — Layer B skipped")
        return []

    places = _places(criteria)
    # Round-robin by template, not by place: with 35 municipalities and a
    # 100-query allowance, grouping by place would cover three towns and
    # ignore the rest of the region.
    genericas = [t.format(place=p) for t in TEMPLATES for p in places]

    # Site-targeted queries go first and are never crowded out: each covers a
    # whole portal, so they buy far more coverage per query than the 37th
    # municipality of a generic template does.
    queries = list(dict.fromkeys(_site_queries(criteria) + genericas))[:allowance]
    log.info("brave: %d queries (allowance %d)", len(queries), allowance)

    already = store.seen_urls()
    candidates: dict[str, str] = {}   # url -> snippet title

    for query in queries:
        data = http.get_json(
            API,
            # Brave's country codes are two-letter and uppercase ("BR"); the
            # first production run sent lowercase and got HTTP 422 on every
            # single query. result_filter is dropped too — it is one more
            # value the API can reject, and we only ever read data["web"], so
            # nothing downstream needs it.
            params={"q": query, "country": "BR", "search_lang": "pt", "count": 20},
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        )
        store.budget_spend(RESOURCE, 1)
        if not data:
            continue
        for result in (data.get("web") or {}).get("results", []):
            url = result.get("url") or ""
            if not url or url in already or url in candidates:
                continue
            if any(host in url for host in SKIP_HOSTS):
                continue
            candidates[url] = result.get("title") or ""

    log.info("brave: %d new candidate pages", len(candidates))
    return _visit(candidates, budgets)


def _visit(candidates: dict[str, str], budgets) -> list[Listing]:
    """Fetch each candidate once and extract. Rules first; the model only sees
    pages the rules could not read, and only when it is switched on."""
    cap = int(budgets.get("max_paginas_novas", 200))
    use_llm = llm_enabled()
    out: list[Listing] = []

    for url, hint in list(candidates.items())[:cap]:
        resp = http.get(url, timeout=20, retries=2)
        if resp is None or "text/html" not in resp.headers.get("content-type", ""):
            continue

        listing = rules.extract(resp.text, url, source=NAME)
        if use_llm and rules.is_thin(listing):
            from ..extract import llm
            better = llm.extract(resp.text, url, source=NAME)
            if better:
                listing = better

        if listing:
            listing.title = listing.title or hint
            out.append(listing)

    log.info("brave: %d listings extracted", len(out))
    return out
