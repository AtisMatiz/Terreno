"""One-off maintenance: recompute score/dimensoes/destaques/estrelas/reasons
for every non-dismissed listing already in the DB, using the current
terreno/scoring.py -- then re-render the site.

Why this exists: scoring only runs once, at extraction time, for a listing
that's freshly fetched (see pipeline.score_all). A fix to scoring.py itself
(e.g. the "construir sua casa" false-positive fixed 2026-09-03) never
touches rows already stored -- they keep whatever score/destaques the old
code computed until the source page is re-fetched, which mostly never
happens again once a URL is in `seen_urls()`. This script is the one-time
catch-up for exactly that: it does NOT re-run motivo_descarte or change
which listings are kept/dismissed, only recomputes the scoring output for
listings already on the site, so a code fix reaches what's already live.

Usage: python scripts/rescore_one_off.py
"""
from __future__ import annotations

import json

from terreno import render, scoring
from terreno.config import DB_PATH, SITE_DIR, load_criteria
from terreno.store import Store


def main() -> None:
    criteria = load_criteria()
    store = Store(DB_PATH)

    pph = criteria.raw.get("preco_por_ha") or {}
    bom = float(pph.get("ideal", scoring.PRECO_HA_BOM))
    limite = float(pph.get("teto_alerta", scoring.PRECO_HA_LIMITE))

    rows = store.db.execute("SELECT * FROM listings WHERE dismissed = 0").fetchall()
    changed = 0
    for row in rows:
        d = dict(row)
        titulo = "" if scoring.titulo_generico(d.get("title")) else (d.get("title") or "")
        text = f"{titulo} {d.get('description') or ''}"

        nota, detalhe, evidencias, estrelas = scoring.avaliar(
            text,
            price_per_ha=d.get("price_per_ha"),
            distancia_centro_km=d.get("distancia_centro_km"),
            preco_ha_bom=bom,
            preco_ha_limite=limite,
            municipality=d.get("municipality") or "",
            centro=criteria.center,
            zona_melhor=criteria.zona_melhor,
            zona_boa=criteria.zona_boa,
            price=d.get("price"),
        )
        _, aviso = scoring.tipo_ok(text)
        novo_destaques = json.dumps(scoring.destaques(detalhe), ensure_ascii=False)
        novo_reasons = "\n".join(([aviso] if aviso else []) + evidencias)
        novo_dimensoes = json.dumps(detalhe, ensure_ascii=False)
        novo_estrelas = json.dumps(estrelas, ensure_ascii=False)

        if (round(nota, 3) != d.get("score") or novo_destaques != d.get("destaques")
                or novo_dimensoes != d.get("dimensoes")):
            store.db.execute(
                "UPDATE listings SET score = ?, dimensoes = ?, reasons = ?, "
                "estrelas = ?, destaques = ? WHERE key = ?",
                (round(nota, 3), novo_dimensoes, novo_reasons, novo_estrelas,
                 novo_destaques, d["key"]),
            )
            changed += 1

    store.db.commit()
    print(f"rescored {len(rows)} listing(s), {changed} changed")

    # Preserve the last real run's sources/warnings banner -- this is a
    # rescore, not a run, so it has none of its own to report.
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
