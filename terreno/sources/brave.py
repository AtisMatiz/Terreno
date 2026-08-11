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
import time
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Hosts already covered by Layer A, plus hosts measured to be unreachable
# from CI outright. Both are wasted budget the same way: fetching them here
# just repeats a failure the pipeline already knows about. wimoveis.com.br
# (403, same as imovelweb) and comprei.pgfn.gov.br (25s connect timeout) were
# added after a production run showed the two PGFN timeouts alone consuming
# most of the 90s visit budget -- candidates from a known-dead host should
# never compete with reachable ones for that time.
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

    # Pendentes de execuções anteriores entram primeiro -- são os candidatos
    # que o orçamento de tempo não deixou visitar da última vez, e sem essa
    # fila persistente seriam descobertos de novo (gastando cota da Brave à
    # toa) e ainda assim nunca alcançados, sempre atropelados pelos ~800 novos
    # candidatos de cada execução. Metade do teto de páginas fica reservada
    # para eles, a outra metade para os candidatos frescos desta execução --
    # sem essa divisão, um backlog grande faria a descoberta de anúncios
    # genuinamente novos parar por completo.
    cap = int(budgets.get("max_paginas_novas", 200))
    limite_pendentes = max(1, cap // 2)
    pendentes_carregados = 0
    for url, dica in store.brave_pendentes_carregar(limite_pendentes):
        if url not in already:
            candidates[url] = dica
            pendentes_carregados += 1
    if pendentes_carregados:
        log.info("brave: %d candidatos pendentes de execuções anteriores",
                 pendentes_carregados)

    for query in queries:
        data = http.get_json(
            API,
            # Brave's country codes are two-letter and uppercase ("BR") --
            # fixed after the first production run sent lowercase. The second
            # run then showed the real culprit in the error body: plain "pt"
            # fails search_lang's enum validation (Brave wants a full locale
            # like "pt-BR", and getting that wrong for every language wasn't
            # worth guessing at, so the parameter is dropped instead -- the
            # query text is already in Portuguese, which does the same job).
            # result_filter is dropped too, for the same reason: one more
            # value the API can reject that nothing downstream needs, since
            # the code only ever reads data["web"].
            params={"q": query, "country": "BR", "count": 20},
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

    novos = len(candidates) - pendentes_carregados
    log.info("brave: %d candidatos pendentes + %d novos = %d no total",
             pendentes_carregados, novos, len(candidates))

    visitados, listings = _visit(candidates, budgets)

    # O que foi tentado (com sucesso ou não) sai da fila; o que sobrou --
    # inclusive candidatos frescos desta execução que nem chegaram a ser
    # tentados -- entra ou permanece, para a próxima execução continuar
    # daqui em vez de começar do zero.
    store.brave_pendentes_remover(visitados)
    nao_visitados = {u: d for u, d in candidates.items() if u not in visitados}
    store.brave_pendentes_adicionar(nao_visitados)
    limite_fila = int(budgets.get("brave_max_fila_pendentes", 2000))
    podados = store.brave_pendentes_podar(limite_fila)
    if podados:
        log.info("brave: %d candidatos antigos descartados da fila (limite %d)",
                 podados, limite_fila)

    return listings


def _visitar_um(url: str, dica: str, use_llm: bool) -> Listing | None:
    """O trabalho de uma única página candidata -- roda em uma thread do pool."""
    resp = http.get(url, timeout=20, retries=2)
    if resp is None or "text/html" not in resp.headers.get("content-type", ""):
        return None

    listing = rules.extract(resp.text, url, source=NAME)
    if use_llm and rules.is_thin(listing):
        from ..extract import llm
        melhor = llm.extract(resp.text, url, source=NAME)
        if melhor:
            listing = melhor

    if listing:
        listing.title = listing.title or dica
    return listing


def _visit(candidates: dict[str, str], budgets) -> tuple[set[str], list[Listing]]:
    """Visita cada candidato em paralelo e extrai o que der. Regras primeiro; o
    modelo só vê páginas que as regras não conseguiram ler, e só quando ligado.

    Limitado de duas formas, não só por contagem: um teto de páginas por si só
    não protege contra alguns sites lentos ou sem resposta entre os
    candidatos, cada um custando 20-40s (timeout x tentativas) sem nada
    impedindo a execução de crescer além do que um job agendado deveria levar.
    O orçamento de tempo é o que de fato mantém a duração previsível.

    Paralelo porque o gargalo é rede, não CPU: ~800 candidatos visitados um a
    um a ~1-3s cada levaria 15-40 minutos; um pool de threads dentro do mesmo
    orçamento de tempo visita muito mais no mesmo período, porque as threads
    passam a maior parte do tempo esperando resposta de servidores diferentes,
    não competindo entre si.

    Retorna as URLs de fato tentadas (para a chamadora tirar da fila) e os
    anúncios extraídos. URLs nunca iniciadas ou canceladas pelo prazo ficam de
    fora de `visitados` -- a chamadora as devolve para a fila, então um lote
    grande é coberto ao longo de várias execuções em vez de recomeçar do zero.
    """
    cap = int(budgets.get("max_paginas_novas", 200))
    prazo_s = int(budgets.get("brave_segundos_max_visita", 90))
    paralelismo = int(budgets.get("brave_paralelismo", 10))
    use_llm = llm_enabled()
    fila = list(candidates.items())[:cap]

    out: list[Listing] = []
    visitados: set[str] = set()
    inicio = time.monotonic()

    with ThreadPoolExecutor(max_workers=paralelismo) as pool:
        futuros = {
            pool.submit(_visitar_um, url, dica, use_llm): url
            for url, dica in fila
        }
        for futuro in as_completed(futuros):
            url = futuros[futuro]
            visitados.add(url)
            try:
                listing = futuro.result()
            except Exception as exc:  # noqa: BLE001 — uma página ruim não pode derrubar a execução
                log.debug("brave: falha ao processar %s: %s", url, exc)
                listing = None
            if listing:
                out.append(listing)

            if time.monotonic() - inicio > prazo_s:
                pendentes = [u for u in futuros.values() if u not in visitados]
                if pendentes:
                    log.info(
                        "brave: prazo de %ds atingido, %d candidatos não concluídos",
                        prazo_s, len(pendentes),
                    )
                # cancel_futures descarta o que ainda nem começou; o que já
                # está em andamento (no máximo `paralelismo` páginas) termina
                # naturalmente, com o próprio timeout de cada requisição como
                # limite -- não fica thread nenhuma rodando depois que a
                # função retorna.
                pool.shutdown(cancel_futures=True)
                break

    log.info("brave: %d listings extraídos de %d/%d candidatos tentados",
             len(out), len(visitados), len(fila))
    return visitados, out
