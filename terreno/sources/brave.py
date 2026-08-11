"""Layer B — long-tail discovery via Brave Search, then visits every
candidate found.

Split into two independent phases on purpose (see `brave_discover.py` and
`brave_visit.py`): discovery spends Brave's metered query budget, visiting
spends only time and network. Those are unrelated constraints that used to
share one artificial deadline -- splitting them means a large backlog of
undiscovered candidates no longer competes with a large backlog of
unvisited ones for the same clock.
"""

from __future__ import annotations

import logging

from .brave_discover import discover
from .brave_visit import visit_all
from .brave_visit import NAME  # re-exported for anything importing it from here

log = logging.getLogger("terreno.sources.brave")


def fetch(criteria, store, budgets) -> list:
    novos = discover(criteria, store, budgets)
    if novos:
        log.info("brave: %d candidatos novos na fila", novos)

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
