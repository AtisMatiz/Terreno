"""Layer B, phase 2 — visits every queued candidate and extracts listings.

Deliberately has no total-time budget. There used to be one (90s, shared
with discovery), added to protect the GitHub Actions job's minutes — but
this repo is public, so Actions minutes are free, and the API's actual
limits (Brave's query quota) apply only to `brave_discover.py`, never to
this module: visiting a candidate page is a plain HTTP GET, unrelated to any
search-API cap. What now bounds a run is the workflow's own
`timeout-minutes`, not an artificial per-run cutoff, so a large backlog gets
visited to completion in one run instead of being rationed across many.

Three outcomes per candidate, handled differently on purpose:
  * extracted a listing        -> done, leave the queue for good.
  * fetched fine, nothing there -> done too; revisiting won't change what a
                                    page that loaded correctly already showed.
  * couldn't even fetch it     -> might be a passing outage, not a dead URL.
                                    Gets a `falhas` counter in the queue and
                                    another chance next run; only discarded
                                    after `brave_max_falhas` such failures.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .. import http
from ..config import llm_enabled
from ..extract import rules
from ..models import Listing
from .brave_discover import SKIP_HOSTS

log = logging.getLogger("terreno.sources.brave_visit")

NAME = "brave"

# Successful extractions from the same host, before it's confident enough to
# call this a specialized real-estate site worth its own weekly site: query
# rotation (see Store.registrar_extracao_brave) rather than a one-off page.
SITES_DESCOBERTOS_LIMIAR = 2

# Never "discover" a host that already has its own dedicated scraper or is
# already hand-curated in sites_alvo (that would just be noise), nor
# facebook.com (handled separately via Apify/cookies, and site: search there
# raises the same ToS concerns the dedicated Facebook source already flags).
_NAO_DESCOBRIR = set(SKIP_HOSTS) | {"facebook.com"}


def _host(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc

# Not a rationing device like the old per-run cap was -- just a backstop so
# a backlog that grew for months without ever being cleared can't make a
# single run try to open an unbounded number of pages.
MAX_POR_EXECUCAO = 5000


def _visitar_um(url: str, dica: str, use_llm: bool, timeout: int) -> tuple[str, Listing | None, str]:
    """O trabalho de uma única página candidata -- roda em uma thread do pool.

    Uma única tentativa por execução (sem retry aqui): o retry mora agora
    no nível da fila, entre execuções -- ver `brave_pendentes_registrar_falha`
    -- porque um site fora do ar agora pode estar de volta amanhã, e duas
    tentativas imediatas em sequência não ajudam nisso, só gastam o dobro do
    tempo desta execução com um site que provavelmente ainda vai falhar.

    Devolve também o corpo da página (truncado): esta é a única vez que o
    texto completo é buscado, então é a melhor chance de `site_categoria`
    achar uma menção a CRECI que o snippet da Brave nunca mostraria.
    """
    resp = http.get(url, timeout=timeout, retries=1)
    if resp is None:
        return "falha_acesso", None, ""
    if "text/html" not in resp.headers.get("content-type", ""):
        return "sem_conteudo", None, ""

    listing = rules.extract(resp.text, url, source=NAME)
    if use_llm and rules.is_thin(listing):
        from ..extract import llm
        melhor = llm.extract(resp.text, url, source=NAME)
        if melhor:
            listing = melhor

    texto = resp.text[:20000]
    if listing:
        listing.title = listing.title or dica
        return "ok", listing, texto
    return "sem_conteudo", None, texto


def visit_all(criteria, store, budgets) -> list[Listing]:
    """Visita todo o backlog pendente em paralelo e extrai o que der. Regras
    primeiro; o modelo só vê páginas que as regras não conseguiram ler, e só
    quando ligado.

    Paralelo porque o gargalo é rede, não CPU: threads passam a maior parte
    do tempo esperando resposta de servidores diferentes, não competindo
    entre si -- então mais paralelismo visita mais páginas no mesmo tempo,
    sem precisar de um teto de tempo artificial para caber num orçamento.
    Dezenas de threads ociosas em I/O não pesam em CPU nem memória do
    runner -- o limite real seria o do site do outro lado, não o nosso.
    """
    sites_alvo = {d.lower() for d in (criteria.raw.get("sites_alvo") or [])}
    paralelismo = int(budgets.get("brave_paralelismo", 50))
    timeout_pagina = int(budgets.get("brave_timeout_pagina_s", 40))
    max_falhas = int(budgets.get("brave_max_falhas", 2))
    use_llm = llm_enabled()
    fila = store.brave_pendentes_carregar(MAX_POR_EXECUCAO)
    if not fila:
        return []

    log.info("brave: visitando %d candidatos pendentes (sem limite de tempo por execução, "
             "%ds por página, %d em paralelo)", len(fila), timeout_pagina, paralelismo)

    out: list[Listing] = []
    textos: dict[str, str] = {}
    sucesso: set[str] = set()
    sem_conteudo: set[str] = set()
    falha_acesso: set[str] = set()

    with ThreadPoolExecutor(max_workers=paralelismo) as pool:
        futuros = {
            pool.submit(_visitar_um, url, dica, use_llm, timeout_pagina): url
            for url, dica in fila
        }
        for futuro in as_completed(futuros):
            url = futuros[futuro]
            try:
                status, listing, texto = futuro.result()
            except Exception as exc:  # noqa: BLE001 — uma página ruim não pode derrubar a execução
                log.debug("brave: falha ao processar %s: %s", url, exc)
                status, listing, texto = "falha_acesso", None, ""

            if status == "ok":
                sucesso.add(url)
                out.append(listing)
                textos[url] = texto
            elif status == "falha_acesso":
                falha_acesso.add(url)
            else:
                sem_conteudo.add(url)

    store.brave_pendentes_remover(sucesso | sem_conteudo)
    descartados = store.brave_pendentes_registrar_falha(falha_acesso, limiar=max_falhas)

    # A successful extraction on a host we didn't already know about is
    # evidence it specializes in real-estate listings -- worth its own
    # site: query rotation once it happens more than once (a single hit could
    # just be one property mentioned on an unrelated page).
    for listing in out:
        host = _host(listing.url)
        if host and host not in _NAO_DESCOBRIR and host not in sites_alvo:
            promovido = store.registrar_extracao_brave(
                host, limiar=SITES_DESCOBERTOS_LIMIAR, texto=textos.get(listing.url, "")
            )
            if promovido:
                log.info("brave: %s promovido a site descoberto -- entra na rotação "
                         "semanal de consultas site:", host)

    log.info(
        "brave: %d listings extraídos de %d/%d candidatos tentados "
        "(%d sem conteúdo, %d falha de acesso -- %d descartados após %d falhas, "
        "%d voltam para a fila)",
        len(out), len(fila), len(fila), len(sem_conteudo), len(falha_acesso),
        len(descartados), max_falhas, len(falha_acesso) - len(descartados),
    )
    return out
