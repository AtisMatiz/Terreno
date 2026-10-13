"""Aplica data/correcoes.json agora e regenera o site.

Disparado por api/editar.js logo após uma edição manual (workflow
aplicar_correcoes.yml), para que a nota global do anúncio seja recalculada
em poucos minutos em vez de esperar a próxima busca agendada. É só a parte
`aplicar_correcoes` + render de `run.py`, sem rodar nenhuma fonte.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terreno import render  # noqa: E402
from terreno.config import DB_PATH, SITE_DIR, load_criteria  # noqa: E402
from terreno.run import aplicar_correcoes  # noqa: E402
from terreno.store import Store  # noqa: E402

log = logging.getLogger("terreno.aplicar_correcoes")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    criteria = load_criteria()
    store = Store(DB_PATH)
    n = aplicar_correcoes(store, criteria)
    log.info("%d anúncio(s) com correção aplicada e nota recalculada", n)

    # Preserva o banner de fontes/avisos do último run real: isto não é um run.
    anterior = json.loads((SITE_DIR / "listings.json").read_text(encoding="utf-8"))
    rows = store.recent(int(criteria.output("manter_dias", 90)))
    render.render(rows, SITE_DIR, new_keys=set(),
                  sources=anterior.get("sources") or [],
                  warnings=anterior.get("warnings") or [], criteria=criteria)
    store.close()


if __name__ == "__main__":
    main()
