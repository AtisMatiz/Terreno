"""Layer B — long-tail discovery via Brave Search + Tavily, then visits every
candidate found.

Split into independent phases on purpose (see `brave_discover.py`,
`tavily_discover.py` and `brave_visit.py`): discovery spends a metered query
budget, visiting spends only time and network. Those are unrelated
constraints that used to share one artificial deadline -- splitting them
means a large backlog of undiscovered candidates no longer competes with a
large backlog of unvisited ones for the same clock.

Two *jobs*, not just two phases, as of 2026-08-17: `fetch` (source name
`brave`, part of the main daily pipeline) does the SDB scan -- Brave's
`site:` rotation for `imobiliaria` hosts, Tavily's batched `include_domains`
for `outro` hosts, then visits everything queued. `fetch_novos` (source name
`brave_novos`) does only the generic new-site hunt, meant to run as its own
separate scheduled job -- see `criteria.yaml`'s `ci_novos` profile and
SESSION_NOTES for why these are two decoupled runs rather than one, and why
that means time-offset, not literally concurrent (both would otherwise try
to commit the same `data/terreno.sqlite3` at once). A candidate `fetch_novos`
queues just waits in `brave_pendentes` for the next `fetch` run to visit it --
the queue is what decouples the two, not a shared deadline.
"""

from __future__ import annotations

import logging

from .brave_discover import discover_novos, discover_sdb
from .brave_visit import visit_all
from .brave_visit import NAME  # re-exported for anything importing it from here
from .tavily_discover import discover as tavily_discover

log = logging.getLogger("terreno.sources.brave")


def fetch(criteria, store, budgets) -> list:
    """SDB scan + visit -- the main daily pipeline's Brave/Tavily work."""
    novos = discover_sdb(criteria, store, budgets)
    if novos:
        log.info("brave: %d candidatos novos na fila", novos)

    novos_tavily = tavily_discover(criteria, store, budgets)
    if novos_tavily:
        log.info("tavily: %d candidatos novos na fila", novos_tavily)

    # visit_all disposes of every candidate it touches itself (removed on
    # success or empty content, given another chance or discarded on a
    # fetch failure) -- there is nothing left for the caller to reconcile.
    listings = visit_all(criteria, store, budgets)

    limite_fila = int(budgets.get("brave_max_fila_pendentes", 2000))
    podados = store.brave_pendentes_podar(limite_fila)
    if podados:
        log.info("brave: %d candidatos antigos descartados da fila (limite %d)",
                 podados, limite_fila)

    return listings


def fetch_novos(criteria, store, budgets) -> list:
    """Generic new-site hunt only -- no visiting, no listings returned.
    Meant to run as its own scheduled job (source name `brave_novos`),
    decoupled from the main pipeline; see module docstring."""
    novos = discover_novos(criteria, store, budgets)
    if novos:
        log.info("brave: %d candidatos novos na fila (descoberta genérica)", novos)
    return []
