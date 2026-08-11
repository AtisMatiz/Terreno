"""Layer B, phase 2 — visits every queued candidate and extracts listings.

Deliberately has no total-time budget. There used to be one (90s, shared
with discovery), added to protect the GitHub Actions job's minutes — but
this repo is public, so Actions minutes are free, and the API's actual
limits (Brave's query quota) apply only to `brave_discover.py`, never to
this module: visiting a candidate page is a plain HTTP GET, unrelated to any
search-API cap. What now bounds a run is the workflow's own
`timeout-minutes`, not an artificial per-run cutoff, so a large backlog gets
visited to completion in one run instead of being rationed across many.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import http
from ..config import llm_enabled
from ..extract import rules
from ..models import Listing

log = logging.getLogger("terreno.sources.brave_visit")

NAME = "brave"

# Not a rationing device like the old per-run cap was -- just a backstop so
# a backlog that grew for months without ever being cleared can't make a
# single run try to open an unbounded number of pages.
MAX_POR_EXECUCAO = 5000


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


def visit_all(store, budgets) -> tuple[set[str], list[Listing]]:
    """Visita todo o backlog pendente em paralelo e extrai o que der. Regras
    primeiro; o modelo só vê páginas que as regras não conseguiram ler, e só
    quando ligado.

    Paralelo porque o gargalo é rede, não CPU: threads passam a maior parte
    do tempo esperando resposta de servidores diferentes, não competindo
    entre si -- então mais paralelismo visita mais páginas no mesmo tempo,
    sem precisar de um teto de tempo artificial para caber num orçamento.

    Retorna as URLs de fato tentadas (para a chamadora tirar da fila -- com
    sucesso ou não, uma página tentada não é retentada indefinidamente) e os
    anúncios extraídos.
    """
    paralelismo = int(budgets.get("brave_paralelismo", 15))
    use_llm = llm_enabled()
    fila = store.brave_pendentes_carregar(MAX_POR_EXECUCAO)
    if not fila:
        return set(), []

    log.info("brave: visitando %d candidatos pendentes (sem limite de tempo por execução)",
             len(fila))

    out: list[Listing] = []
    visitados: set[str] = set()

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

    log.info("brave: %d listings extraídos de %d/%d candidatos tentados",
             len(out), len(visitados), len(fila))
    return visitados, out
