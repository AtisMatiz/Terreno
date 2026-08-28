"""SDB (`sites_descobertos`) opportunity scan -- the whole thing, both
categories (see `terreno/site_categoria.py`), as of 2026-08-17.

A real benchmark that day (`scripts/diagnostico_imobiliaria_crawl.py`, 30
real `imobiliaria` hosts) measured Tavily against the no-API crawler
(`imobiliaria_crawl.py`): Tavily found 2.6x more usable listings and ran 40%
faster on average. So Tavily now covers the daily rotation for *every* SDB
host, `imobiliaria` included -- Brave's `site:` rotation for `imobiliaria`
(what this module used to leave to `brave_discover.py`) is retired entirely,
freeing Brave's metered budget for `brave_discover.discover_novos` (the
generic new-site hunt) alone. `imobiliaria_crawl.py` isn't wasted, though: the
same benchmark showed it still wins on a few hosts Tavily misses entirely, so
it runs as its own twice-weekly safety-net job instead (see
`search_crawl_imobiliaria.yml`) -- belt and suspenders, not either/or.

Tavily's `include_domains` takes up to 300 domains in a single call
(confirmed against Tavily's own API reference), against Brave's `site:` text
trick, one host per query -- so this batches every due host, across both
categories, into as few calls as `_LOTE` allows.

Deliberately mirrors `brave_discover.py`'s shape (same queue table via
`store.brave_pendentes_adicionar`, same weekly-due gate, same key-2 fallback
pattern as `BRAVE_API_KEY_2`/`APIFY_TOKEN_2`) so the two modules stay easy to
compare and `run.py` can call either the same way.
"""

from __future__ import annotations

import logging

import requests

from .. import http
from ..site_categoria import IMOBILIARIA, OUTRO

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
    """Queries Tavily for every SDB host due this week -- both categories,
    batched -- and queues every new candidate URL for `brave_visit.py` to
    open, same queue and extraction path as a Brave-found candidate. Returns
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

    # Seeds sites_alvo (curated portals) into the same weekly rotation
    # auto-discovered hosts already use -- side effect only, moved here
    # 2026-08-17 from brave_discover.py along with the rotation itself.
    sites = criteria.raw.get("sites_alvo") or []
    if sites:
        store.sites_alvo_semear(sites)

    # `TAVILY_SDB_VARREDURA_COMPLETA=1` ignora o cooldown de 7 dias e consulta
    # TODO host promovido nesta execução -- usado para a varredura pontual de
    # comparação Tavily x crawler sem API (ver SESSION_NOTES 2026-08-24), não
    # para uso semanal normal. `dias=0` faz `ultima_consulta < agora` valer
    # pra qualquer timestamp passado, então todo host promovido conta como
    # vencido.
    dias = 0 if env("TAVILY_SDB_VARREDURA_COMPLETA") else 7
    hosts = (store.sites_descobertos_por_categoria(IMOBILIARIA, dias=dias)
             + store.sites_descobertos_por_categoria(OUTRO, dias=dias))
    if not hosts:
        log.info("tavily: nenhum host da SDB vencido nesta semana")
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

    # origem="tavily" (2026-08-28): lets the resulting listings' `source`
    # field say "tavily" instead of the shared queue's old blanket "brave",
    # so a Tavily-vs-Brave comparison can be run directly off the listings
    # table instead of needing a one-off diagnostic script -- see the
    # 2026-08-28 Friday SDB sweep analysis in SESSION_NOTES.md for why this
    # was missing until now.
    store.brave_pendentes_adicionar(novos, origem="tavily")
    if hosts_consultados:
        store.sites_descobertos_marcar_consultado(hosts_consultados)
    log.info("tavily: %d host(s) da SDB consultados em %d lote(s), %d candidato(s) novo(s) na fila",
              len(hosts_consultados), len(lotes), len(novos))
    return len(novos)
