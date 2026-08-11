"""Layer B, phase 1 — long-tail discovery via the Brave Search API.

This phase only queries Brave and queues whatever URLs it finds; it never
opens a candidate page itself. That split matters because the two phases are
bound by unrelated constraints: this one spends Brave's metered, capped free
tier (queries/month), while phase 2 (`brave_visit.py`) spends nothing but
time and network on plain HTTP fetches. They used to share one deadline,
which meant a large backlog of already-discovered candidates permanently
crowded out — or was crowded out by — new discovery. Queuing everything here
and visiting the queue separately removes that coupling.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timezone

from .. import http
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.brave_discover")

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

# Hosts already covered by Layer A, plus hosts measured to be unreachable
# from CI outright. Both are wasted effort the same way: fetching them here
# just repeats a failure the pipeline already knows about. wimoveis.com.br
# (403, same as imovelweb) and comprei.pgfn.gov.br (25s connect timeout) were
# added after a production run showed the two PGFN timeouts alone consuming
# most of the old visit budget -- irrelevant now that visiting has no shared
# deadline to protect, but they are still dead weight worth skipping.
SKIP_HOSTS = (
    "olx.com.br", "vivareal.com.br", "zapimoveis.com.br",
    "mercadolivre.com.br", "chavesnamao.com.br", "imovelweb.com.br",
    "wimoveis.com.br", "comprei.pgfn.gov.br",
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


def _alvo(criteria) -> str:
    uf = criteria.states[0] if criteria.states else ""
    return criteria.regiao or (UF_NAMES.get(uf, uf).replace("-", " ") if uf else "")


def _site_queries(criteria) -> list[str]:
    """One `site:` query per target domain, aimed at the region (or state).

    This is how the search covers dozens of portals and agencies without a
    scraper for each. Brave is an API with a key, so these reach sites that
    refuse our datacenter IP directly — including wimoveis and imovelweb, which
    answer 403 to the scrapers but are perfectly readable through search.
    """
    sites = criteria.raw.get("sites_alvo") or []
    alvo = _alvo(criteria)
    if not sites or not alvo:
        return []
    return [f"fazenda OR sítio OR chácara à venda {alvo} site:{d}" for d in sites]


def _discovered_site_queries(criteria, store, dias: int = 7) -> dict[str, str]:
    """`site:` queries for hosts `brave_visit.py` auto-discovered (see
    `Store.registrar_extracao_brave`) rather than hand-curated in `sites_alvo`.

    Returns {query: host} so the caller can tell, after truncating the
    combined query list to the run's allowance, exactly which discovered
    hosts actually got queried this run — only those get their weekly clock
    (`ultima_consulta`) reset.
    """
    alvo = _alvo(criteria)
    if not alvo:
        return {}
    hosts = store.sites_descobertos_para_consultar(dias=dias)
    return {f"fazenda OR sítio OR chácara à venda {alvo} site:{h}": h for h in hosts}


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


def discover(criteria, store, budgets) -> int:
    """Query Brave and queue every new candidate URL for phase 2 to visit.

    Returns how many new candidates were queued. Never fetches a candidate
    page itself — that is entirely `brave_visit.py`'s job.
    """
    from ..config import env
    token = env("BRAVE_API_KEY")
    if not token:
        log.warning("BRAVE_API_KEY not set — Brave discovery skipped")
        return 0

    allowance = _daily_allowance(
        store,
        int(budgets.get("brave_consultas_por_run", 50)),
        int(budgets.get("brave_consultas_por_mes", 2000)),
    )
    if allowance <= 0:
        log.warning("brave: monthly query budget exhausted — discovery skipped")
        return 0

    places = _places(criteria)
    # Round-robin by template, not by place: with 35 municipalities and a
    # 100-query allowance, grouping by place would cover three towns and
    # ignore the rest of the region.
    genericas = [t.format(place=p) for t in TEMPLATES for p in places]

    # Auto-discovered sites (brave_visit.py promoted them after repeated real
    # extractions) get the same "whole portal" priority as the hand-curated
    # sites_alvo ones, just on their own weekly clock rather than every run.
    descobertos = _discovered_site_queries(criteria, store)

    # Site-targeted queries go first and are never crowded out: each covers a
    # whole portal, so they buy far more coverage per query than the 37th
    # municipality of a generic template does.
    queries = list(dict.fromkeys(
        _site_queries(criteria) + list(descobertos) + genericas
    ))[:allowance]
    log.info("brave: %d queries (allowance %d)", len(queries), allowance)

    consultados_agora = {host for q, host in descobertos.items() if q in queries}
    if consultados_agora:
        store.sites_descobertos_marcar_consultado(consultados_agora)
        log.info("brave: %d site(s) descoberto(s) consultados nesta rodada semanal: %s",
                 len(consultados_agora), ", ".join(sorted(consultados_agora)))

    already = store.seen_urls()
    novos: dict[str, str] = {}

    for query in queries:
        data = http.get_json(
            API,
            # Brave's country codes are two-letter and uppercase ("BR").
            # search_lang and result_filter are both omitted: Brave's enum
            # validation rejected "pt" for search_lang (it wants a full
            # locale like "pt-BR", not worth guessing at for every language),
            # and nothing downstream reads result_filter anyway since the
            # code only ever reads data["web"].
            params={"q": query, "country": "BR", "count": 20},
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        )
        store.budget_spend(RESOURCE, 1)
        if not data:
            continue
        for result in (data.get("web") or {}).get("results", []):
            url = result.get("url") or ""
            if not url or url in already or url in novos:
                continue
            if any(host in url for host in SKIP_HOSTS):
                continue
            novos[url] = result.get("title") or ""

    store.brave_pendentes_adicionar(novos)
    log.info("brave: %d candidatos novos na fila de visita", len(novos))
    return len(novos)
