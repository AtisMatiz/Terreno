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
from urllib.parse import urlparse

import requests

from .. import http
from ..site_categoria import IMOBILIARIA
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

# Hosts already covered by Layer A. Fetching them here just repeats a
# failure (or a success) the pipeline already knows about from its own
# dedicated source module.
SKIP_HOSTS = (
    "olx.com.br", "vivareal.com.br", "zapimoveis.com.br",
    "mercadolivre.com.br", "chavesnamao.com.br", "comprei.pgfn.gov.br",
)

# Not covered by a dedicated source, but measured (2026-08-13) to serve a
# real Cloudflare JS challenge to every free transport tried, including a
# self-hosted headless browser -- see SESSION_NOTES. Still worth *finding*
# through Brave/Tavily (the URL + whatever the search snippet shows is
# strictly better than nothing), but never worth a live visit attempt right
# now: that would just burn the visit's time on an outcome already known.
# Results from these hosts go straight to `brave_frios` (cold storage)
# instead of the active `brave_pendentes` queue -- archived, not lost, so a
# working transport later can use them without re-spending a search query.
COLD_HOSTS = ("wimoveis.com.br", "imovelweb.com.br")


def _host(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


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


def _site_queries(criteria, store) -> list[str]:
    """One `site:` query per target domain, aimed at the region (or state).

    This is how the search covers dozens of portals and agencies without a
    scraper for each. Brave is an API with a key, so these reach sites that
    refuse our datacenter IP directly — including wimoveis and imovelweb, which
    answer 403 to the scrapers but are perfectly readable through search.

    2026-08-13: these used to be queried on *every single run*, forever,
    regardless of whether anything about a slow-moving rural-land inventory
    could plausibly have changed since yesterday -- real waste against a
    metered API with a hard monthly cap (see Standing rules, the day Brave's
    cap was hit). Now seeded into the same `sites_descobertos` table the
    auto-discovered hosts already use, so a curated site shares their
    weekly cadence (`sites_descobertos_por_categoria`) instead of its own
    unconditional one -- one mechanism, not two.
    """
    sites = criteria.raw.get("sites_alvo") or []
    if sites:
        store.sites_alvo_semear(sites)
    return []


def _discovered_site_queries(criteria, store, dias: int = 7) -> dict[str, str]:
    """`site:` queries for `imobiliaria`-category hosts due this week --
    both hand-curated (`sites_alvo`, seeded by `_site_queries`, always
    `imobiliaria`) and auto-discovered (see `Store.registrar_extracao_brave`),
    unified under one weekly clock.

    Scoped to `imobiliaria` only (2026-08-17): `outro`-category hosts are
    now `tavily_discover.py`'s job -- batched into Tavily's `include_domains`
    instead of one Brave `site:` query per host, see that module's docstring.
    Querying them here too would waste Brave's metered budget on the exact
    hosts Tavily is meant to take off it.

    Returns {query: host} so the caller can tell, after truncating the
    combined query list to the run's allowance, exactly which discovered
    hosts actually got queried this run — only those get their weekly clock
    (`ultima_consulta`) reset.
    """
    alvo = _alvo(criteria)
    if not alvo:
        return {}
    hosts = store.sites_descobertos_por_categoria(IMOBILIARIA, dias=dias)
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


def _tokens():
    from ..config import env
    return env("BRAVE_API_KEY"), env("BRAVE_API_KEY_2")


def discover_novos(criteria, store, budgets) -> int:
    """Generic new-site hunt only -- the `TEMPLATES` loop, aimed at finding
    hosts `sites_descobertos` doesn't have yet. Split out (2026-08-17) into
    its own standalone run (see `brave.fetch_novos`, `REGISTRY["brave_novos"]`)
    so it can be scheduled as its own daily job, decoupled from the SDB scan
    (`discover_sdb`) and from visiting anything found -- a candidate queued
    here just waits in `brave_pendentes` for the next `brave_visit.py` pass,
    same as it always has, regardless of which job queued it.

    Returns how many new candidates were queued.
    """
    token, token2 = _tokens()
    if not token and not token2:
        log.warning("BRAVE_API_KEY not set — Brave discovery skipped")
        return 0

    allowance = _daily_allowance(
        store,
        int(budgets.get("brave_consultas_por_run_novos", budgets.get("brave_consultas_por_run", 50))),
        int(budgets.get("brave_consultas_por_mes", 2000)),
    )
    if allowance <= 0:
        log.warning("brave: monthly query budget exhausted — discovery skipped")
        return 0

    places = _places(criteria)
    # Round-robin by template, not by place: with 35 municipalities and a
    # 100-query allowance, grouping by place would cover three towns and
    # ignore the rest of the region.
    queries = [t.format(place=p) for t in TEMPLATES for p in places][:allowance]
    log.info("brave: %d queries de descoberta genérica (allowance %d)", len(queries), allowance)

    # Hosts the sites database already knows about (curated + auto-
    # discovered, promoted or not). The generic templates' whole job is to
    # find websites we *don't* have yet -- a site: query (discover_sdb)
    # already covers known hosts on their own weekly clock, so a generic-
    # query hit on a host we already track is redundant with that channel,
    # not a new find.
    conhecidos = set(SKIP_HOSTS) | set(COLD_HOSTS) | store.hosts_conhecidos()
    already = store.seen_urls()
    novos: dict[str, str] = {}
    frios: dict[str, str] = {}
    hosts_novos: set[str] = set()
    titulos_novos: dict[str, str] = {}
    ja_conhecidos = 0

    for query in queries:
        data = _consultar(query, token, token2)
        store.budget_spend(RESOURCE, 1)
        if not data:
            continue
        for result in (data.get("web") or {}).get("results", []):
            url = result.get("url") or ""
            if not url or url in already or url in novos or url in frios:
                continue
            if any(host in url for host in SKIP_HOSTS):
                continue
            host = _host(url)
            if host in conhecidos:
                # Already in the sites database -- this query slot found a
                # site we already have, not a new one. Still worth a visit
                # in principle, but that's exactly what the site: rotation
                # is for; here it just means don't spend discovery budget
                # re-confirming a host we already track.
                ja_conhecidos += 1
                continue
            titulo = result.get("title") or ""
            if any(h in url for h in COLD_HOSTS):
                # Known blocked today (see COLD_HOSTS) -- archive straight
                # to cold storage rather than a live visit whose outcome is
                # already known, and keep the description too: it is the
                # only information we will ever have about this URL until a
                # working transport exists.
                descricao = result.get("description") or ""
                frios[url] = f"{titulo} — {descricao}"[:500] if descricao else titulo
                continue
            novos[url] = titulo
            hosts_novos.add(host)
            if host not in titulos_novos:
                # Title + snippet description is the only text about this
                # host that exists before anything is ever visited -- feeds
                # site_categoria's first classification pass.
                descricao = result.get("description") or ""
                titulos_novos[host] = f"{titulo} {descricao}".strip()

    store.brave_pendentes_adicionar(novos)
    if hosts_novos:
        store.sites_descobertos_avistar(hosts_novos, titulos_novos)
        log.info("brave: %d site(s) novo(s) achado(s) pela busca genérica -> sites_descobertos: %s",
                 len(hosts_novos), ", ".join(sorted(hosts_novos)))
    if ja_conhecidos:
        log.info("brave: %d resultado(s) de host(s) já conhecido(s) ignorados na busca genérica "
                 "(cobertos pela rotação site:, não gastos aqui de novo)", ja_conhecidos)
    if frios:
        n = store.brave_frios_adicionar(frios, motivo="desafio JS conhecido (ver COLD_HOSTS)")
        log.info("brave: %d candidato(s) de host(s) conhecidos bloqueados -> frios (%d novos)",
                 len(frios), n)
    log.info("brave: %d candidatos novos na fila de visita", len(novos))
    return len(novos)


def discover_sdb(criteria, store, budgets) -> int:
    """SDB scan: the `site:` rotation over `imobiliaria`-category hosts due
    this week (see `_discovered_site_queries`) -- both hand-curated
    (`sites_alvo`) and auto-discovered. Runs as part of the main daily
    pipeline (`brave.fetch`), alongside `tavily_discover.discover` (the
    `outro`-category half of the same rotation) and `brave_visit.visit_all`.

    Every query here targets a host already in `sites_descobertos` by
    construction, so none of `discover_novos`'s "is this a new site"
    bookkeeping applies -- a result here either becomes a queued candidate
    or, on a known-blocked host, goes straight to cold storage.

    Returns how many new candidates were queued.
    """
    token, token2 = _tokens()
    if not token and not token2:
        log.warning("BRAVE_API_KEY not set — Brave SDB scan skipped")
        return 0

    allowance = _daily_allowance(
        store,
        int(budgets.get("brave_consultas_por_run", 50)),
        int(budgets.get("brave_consultas_por_mes", 2000)),
    )
    if allowance <= 0:
        log.warning("brave: monthly query budget exhausted — SDB scan skipped")
        return 0

    # Seeds sites_alvo into the same weekly rotation auto-discovered hosts
    # use (see _site_queries's docstring) -- side effect only.
    _site_queries(criteria, store)
    descobertos = _discovered_site_queries(criteria, store)
    queries = list(descobertos)[:allowance]
    log.info("brave: %d queries de rotação site: (allowance %d)", len(queries), allowance)

    consultados_agora = {host for q, host in descobertos.items() if q in queries}
    if consultados_agora:
        store.sites_descobertos_marcar_consultado(consultados_agora)
        log.info("brave: %d site(s) descoberto(s) consultados nesta rodada semanal: %s",
                 len(consultados_agora), ", ".join(sorted(consultados_agora)))

    already = store.seen_urls()
    novos: dict[str, str] = {}
    frios: dict[str, str] = {}

    for query in queries:
        data = _consultar(query, token, token2)
        store.budget_spend(RESOURCE, 1)
        if not data:
            continue
        for result in (data.get("web") or {}).get("results", []):
            url = result.get("url") or ""
            if not url or url in already or url in novos or url in frios:
                continue
            if any(host in url for host in SKIP_HOSTS):
                continue
            titulo = result.get("title") or ""
            if any(h in url for h in COLD_HOSTS):
                descricao = result.get("description") or ""
                frios[url] = f"{titulo} — {descricao}"[:500] if descricao else titulo
                continue
            novos[url] = titulo

    store.brave_pendentes_adicionar(novos)
    if frios:
        n = store.brave_frios_adicionar(frios, motivo="desafio JS conhecido (ver COLD_HOSTS)")
        log.info("brave: %d candidato(s) de host(s) conhecidos bloqueados -> frios (%d novos)",
                 len(frios), n)
    log.info("brave: %d candidatos novos na fila de visita (rotação site:)", len(novos))
    return len(novos)


def _consultar(query: str, token: str, token2: str = ""):
    """One Brave query, JSON or None. Tries `token`; on a 402 (monthly spend
    cap exhausted -- exactly what happened 2026-08-13) falls back to `token2`
    (a second account) instead of silently going quiet for the rest of the
    run. Bypasses `http.get_json` on purpose: that layer collapses every
    non-200 into a bare None, and telling "cap exhausted, try key 2" apart
    from "bad request" needs the real status code.
    """
    for i, tok in enumerate(t for t in (token, token2) if t):
        try:
            r = http._session.get(
                API, params={"q": query, "country": "BR", "count": 20},
                headers={"X-Subscription-Token": tok, "Accept": "application/json"},
                timeout=25,
            )
        except requests.RequestException as exc:
            log.warning("api.search.brave.com: %s: %s", type(exc).__name__, exc)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.warning("api.search.brave.com: response was not JSON")
                return None
        if r.status_code == 402 and i == 0 and token2:
            log.warning("brave: cota mensal esgotada na chave principal — "
                        "tentando BRAVE_API_KEY_2")
            continue
        log.warning("api.search.brave.com: HTTP %s — %s",
                    r.status_code, (r.text or "")[:300])
        return None
    return None
