"""Verificação mensal: dos anúncios já publicados no site, quais saíram do ar?

Por que isto existe: `disponibilidade.urls_indisponiveis` já roda a cada
execução normal (`run.py`), mas só sobre `para_avisar` -- os candidatos
prestes a virar notificação nova. Um anúncio que já está no site há semanas
nunca mais passa por ali: uma vez publicado, nada o revisita, então "vendido
há um mês" só vira visível quando alguém clica no link e leva um 404 (como
aconteceu, 2026-09-25, com dois anúncios do OLX). Este script é essa segunda
passada, sobre TODO anúncio ainda ativo no site, não só os recém-descobertos.

Uso: python scripts/verificar_disponibilidade.py
Agendamento: .github/workflows/verificar_disponibilidade.yml, mensal.
"""

from __future__ import annotations

import json
import logging

from terreno import disponibilidade, render
from terreno.config import DB_PATH, SITE_DIR, load_criteria
from terreno.store import Store

log = logging.getLogger("terreno.verificar_disponibilidade")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    criteria = load_criteria()
    store = Store(DB_PATH)

    rows = store.db.execute(
        "SELECT key, url FROM listings WHERE dismissed = 0"
    ).fetchall()
    url_para_key = {row["url"]: row["key"] for row in rows if row["url"]}
    log.info("verificando %d anúncio(s) ativo(s)", len(url_para_key))

    # Paralelismo bem acima do padrão (8, pensado para a dúzia de candidatos
    # de um run normal): aqui são potencialmente centenas de URLs, em muitos
    # hosts diferentes, então mais trabalhadores simultâneos ainda respeita o
    # intervalo mínimo por host (terreno.http já garante isso por conta
    # própria) sem alongar a verificação por horas.
    indisponiveis = disponibilidade.urls_indisponiveis(
        url_para_key.keys(), paralelismo=20,
    )
    keys_para_remover = [url_para_key[u] for u in indisponiveis]

    removidos = store.dismiss_many(keys_para_remover)
    log.info("%d anúncio(s) fora do ar, removido(s) do site", removidos)

    if removidos:
        # Mesmo formato do `rescore_one_off.py`: preserva o banner de
        # sources/warnings do último run real, já que esta é uma verificação
        # de manutenção, não um run, e não tem nada próprio a reportar ali.
        anterior = json.loads((SITE_DIR / "listings.json").read_text(encoding="utf-8"))
        fresh_rows = store.recent(int(criteria.output("manter_dias", 90)))
        render.render(
            fresh_rows, SITE_DIR,
            new_keys=set(),
            sources=anterior.get("sources") or [],
            warnings=anterior.get("warnings") or [],
            criteria=criteria,
        )

    store.close()


if __name__ == "__main__":
    main()
