"""SDB (`sites_descobertos`) opportunity scan for hosts classified `outro`
(see `terreno/site_categoria.py`) -- everything that isn't a real-estate
agency/portal (blogs, city-hall pages, forums, syndicates), so a crawler has
no predictable "listings for sale" structure to exploit and a search API is
still the only practical way in.

Uses Tavily instead of Brave for this segment specifically: Tavily's
`include_domains` takes up to 300 domains in a single call (confirmed against
Tavily's own API reference, 2026-08-17), while Brave only offers the `site:`
text trick, one host per query. So instead of one query per due host -- what
`brave_discover._discovered_site_queries` still does for `imobiliaria` hosts
-- this batches every due `outro` host into as few calls as `_LOTE` allows,
turning what would be dozens of Brave queries into a handful of Tavily ones.

Deliberately mirrors `brave_discover.py`'s shape (same queue table via
`store.brave_pendentes_adicionar`, same weekly-due gate, same key-2 fallback
pattern as `BRAVE_API_KEY_2`/`APIFY_TOKEN_2`) so the two modules stay easy to
compare and `run.py` can call either the same way.
"""

from __future__ import annotations

import logging

import requests

from .. import http
from ..site_categoria import OUTRO

log = logging.getLogger("terreno.sources.tavily_discover")

API = "https://api.tavily.com/search"
RESOURCE = "tavily_queries"

# Tavily's documented ceiling for include_domains (2026-08-17, per Tavily's
# own API reference) -- used at the max since a wrong/empty result on one
# batch costs the same 1 credit whether it covers 50 hosts or 300.
_LOTE = 300
MAX_RESULTS = 20

QUERY = "sítio OR fazenda OR chácara à venda hectares"


def _alvo(criteria) -> str:
    from .base import UF_NAMES
    uf = criteria.states[0] if criteria.states else ""
    return criteria.regiao or (UF_NAMES.get(uf, uf).replace("-", " ") if uf else "")


def _consultar(query: str, domains: list[str], token: str, token2: str = ""):
    """One Tavily call across up to `_LOTE` domains at once. Same 402-then-
    key-2 shape as `brave_discover._consultar`, adapted to Tavily's response
    codes (429 is Tavily's actual rate/quota-exceeded status, not 402)."""
    for i, tok in enumerate(t for t in (token, token2) if t):
        try:
            r = http._session.post(
                API,
                json={"api_key": tok, "query": query, "include_domains": domains,
                      "max_results": MAX_RESULTS, "search_depth": "basic"},
                timeout=30,
            )
        except requests.RequestException as exc:
            log.warning("api.tavily.com: %s: %s", type(exc).__name__, exc)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.warning("api.tavily.com: response was not JSON")
                return None
        if r.status_code in (429, 432, 433) and i == 0 and token2:
            log.warning("tavily: cota esgotada na chave principal — tentando TAVILY_API_KEY_2")
            continue
        log.warning("api.tavily.com: HTTP %s — %s", r.status_code, (r.text or "")[:300])
        return None
    return None


def discover(criteria, store, budgets) -> int:
    """Queries Tavily for every `outro`-category SDB host due this week,
    batched, and queues every new candidate URL for `brave_visit.py` to open
    -- same queue, same extraction path as a Brave-found candidate. Returns
    how many new candidates were queued."""
    from ..config import env
    token = env("TAVILY_API_KEY")
    token2 = env("TAVILY_API_KEY_2")
    if not token and not token2:
        log.info("TAVILY_API_KEY not set — Tavily SDB scan skipped")
        return 0

    alvo = _alvo(criteria)
    if not alvo:
        return 0

    hosts = store.sites_descobertos_por_categoria(OUTRO, dias=7)
    if not hosts:
        log.info("tavily: nenhum host 'outro' vencido nesta semana")
        return 0

    cap = float(budgets.get("tavily_consultas_por_mes", 900))
    query = f"{QUERY} {alvo}".strip()

    already = store.seen_urls()
    novos: dict[str, str] = {}
    hosts_consultados: set[str] = set()

    lotes = [hosts[i:i + _LOTE] for i in range(0, len(hosts), _LOTE)]
    for lote in lotes:
        if store.budget_remaining(RESOURCE, cap) < 1:
            log.warning("tavily: cota mensal de consultas esgotada — %d host(s) restantes ficam "
                        "para a próxima execução", len(hosts) - len(hosts_consultados))
            break
        data = _consultar(query, lote, token, token2)
        store.budget_spend(RESOURCE, 1)
        hosts_consultados.update(lote)
        if not data:
            continue
        for result in data.get("results", []):
            url = result.get("url") or ""
            if url and url not in already and url not in novos:
                novos[url] = result.get("title") or ""

    store.brave_pendentes_adicionar(novos)
    if hosts_consultados:
        store.sites_descobertos_marcar_consultado(hosts_consultados)
    log.info("tavily: %d host(s) 'outro' consultados em %d lote(s), %d candidato(s) novo(s) na fila",
              len(hosts_consultados), len(lotes), len(novos))
    return len(novos)
