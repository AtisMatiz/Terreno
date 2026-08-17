"""Entry point. One invocation does the whole run:

    python -m terreno.run [--only SOURCE] [--dry-run]

Every source is isolated: one portal blocking or changing its markup costs that
source's listings and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import disponibilidade, http, notify, pipeline, render
from .config import (DB_PATH, SITE_DIR, env, llm_enabled, load_criteria,
                     salvar_criterios)
from .sources import REGISTRY
from .store import Store

log = logging.getLogger("terreno")

# Consecutive failed runs before a source's silence becomes a Telegram alert
# rather than just a line in the workflow log nobody reads until asked to.
LIMIAR_ALERTA_SAUDE = 3

# Discovery-only sources (2026-08-17): success here is candidates queued into
# `brave_pendentes`, not Listings returned -- fetch_novos always returns []
# by design (see terreno/sources/brave.py), so the usual "zero results looks
# like a block" heuristic below would flag it as unhealthy on every single
# run, including the many perfectly normal ones that just found no new host
# that day. Exempted from that heuristic entirely; a real failure still
# surfaces via `error` from run_source, same as any other source.
_SEM_HEALTH_POR_CONTAGEM = {"brave_novos"}


def run_source(name: str, fetch, criteria, store, budgets) -> tuple[list, str | None]:
    try:
        return fetch(criteria, store, budgets), None
    except Exception as exc:  # noqa: BLE001 — a broken source must not end the run
        log.exception("source %s failed", name)
        return [], f"{name}: {type(exc).__name__}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="terreno")
    parser.add_argument("--only", help="run a single source by name")
    parser.add_argument("--profile", help="run the named profile from criteria.yaml "
                                          "(ci, local)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and score, but do not write the database or page")
    parser.add_argument("--salvar-criterios", action="store_true",
                        help="grava os critérios efetivos (incluindo os de "
                             "TERRENO_OVERRIDES) de volta em criteria.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    criteria = load_criteria()
    if getattr(args, "salvar_criterios", False):
        salvar_criterios(criteria)
        log.info("critérios efetivos gravados em criteria.yaml")
    store = Store(DB_PATH)
    budgets = criteria.budgets()
    warnings: list[str] = []

    if args.only:
        wanted = [args.only]
    else:
        profile = criteria.profile(args.profile)
        if args.profile and profile is None:
            log.error("profile %r not defined in criteria.yaml", args.profile)
            return 1
        wanted = [
            name for name in REGISTRY
            if criteria.source_enabled(name)
            and (profile is None or name in profile)
        ]
    if not wanted:
        log.error("no sources enabled in criteria.yaml")
        return 1
    log.info("sources: %s | LLM: %s", ", ".join(wanted),
             "on" if llm_enabled() else "off")

    collected = []
    saude: dict[str, bool] = {}
    for name in wanted:
        fetch = REGISTRY.get(name)
        if not fetch:
            warnings.append(f"{name}: unknown source")
            continue
        found, error = run_source(name, fetch, criteria, store, budgets)
        if error:
            warnings.append(error)
            saude[name] = False
        elif not found and name not in _SEM_HEALTH_POR_CONTAGEM:
            # A heuristic, not a certainty: a source with real coverage
            # returning literally zero across a whole state/region is far
            # more likely blocked than genuinely empty, which is what makes
            # this a useful signal despite the false-positive risk on a
            # narrow, legitimately quiet search.
            warnings.append(f"{name}: 0 resultados")
            saude[name] = False
        else:
            saude[name] = True
        collected.extend(found)

    log.info("collected %d raw listings", len(collected))

    normalized = [pipeline.normalize(item) for item in collected]
    deduped = pipeline.dedup(normalized)
    filtered = pipeline.apply_filters(deduped, criteria, store)
    enriched = pipeline.enrich(filtered, budgets)
    # Second pass, deliberately: sources that read price and area from a URL
    # slug carry no municipality yet on the first pass, so the region and radius
    # filters would silently never apply to them. Enrichment fills those in.
    refiltered = pipeline.apply_filters(enriched, criteria, store)
    scored = pipeline.score_all(refiltered, criteria)
    log.info("%d brutos -> %d sem duplicata -> %d filtrados -> %d após enriquecer -> %d pontuados",
             len(normalized), len(deduped), len(filtered), len(refiltered), len(scored))

    for host in http.blocked_hosts():
        warnings.append(f"{host}: bloqueado")

    if args.dry_run:
        for item in scored[:20]:
            print(f"{item.score:5.2f}  {str(item.price):>10}  "
                  f"{str(item.area_ha):>8} ha  {item.municipality}/{item.uf}  {item.url}")
        store.close()
        return 0

    # Health tracking writes to the database, so it stays out of --dry-run --
    # a preview run must not affect the alert streak a real run would see.
    alertas_saude = []
    for name, ok in saude.items():
        streak = store.health_update(name, ok)
        if streak >= LIMIAR_ALERTA_SAUDE:
            alertas_saude.append(f"{name}: sem resultado há {streak} execuções seguidas")

    # Photo reading runs only on the shortlist, before storing, so the vision
    # evidence it adds is persisted with everything else. The score cutoff is
    # applied inside -- see pipeline.enriquecer_imagens for why it lives there.
    pipeline.enriquecer_imagens(scored, criteria)

    fresh = []
    for item in scored:
        stored = store.upsert(item)
        if stored.is_new:
            fresh.append(stored)
    store.db.commit()

    rows = store.recent(int(criteria.output("manter_dias", 90)))
    render.render(
        rows, SITE_DIR,
        new_keys={item.key for item in fresh},
        sources=wanted,
        warnings=warnings,
        criteria=criteria,
    )

    summary = f"{len(scored)} matches, {len(fresh)} new"
    if warnings:
        summary += f" | warnings: {'; '.join(warnings)}"
    store.record_run(summary)
    log.info(summary)

    # Everything scored reaches the page; the Telegram list is deliberately
    # narrower. Two gates, both meant to keep the channel worth reading rather
    # than to hide listings: price per hectare above the ceiling
    # (`notificavel`, set in pipeline.score_all) and anything already sold or
    # taken down. Both leave the listing on the site.
    para_avisar = [item for item in fresh if item.notificavel]
    caros = len(fresh) - len(para_avisar)
    if caros:
        log.info("%d anúncio(s) novo(s) acima do teto de R$/ha — no site, "
                 "fora do Telegram", caros)

    if para_avisar and criteria.output("checar_disponibilidade", True):
        fora = disponibilidade.urls_indisponiveis(x.url for x in para_avisar)
        if fora:
            for item in para_avisar:
                if item.url in fora:
                    item.disponibilidade = "indisponivel"
            para_avisar = [x for x in para_avisar if x.url not in fora]
            log.info("%d anúncio(s) já vendido(s) ou fora do ar — não avisados",
                     len(fora))

    # Called every real (non-dry-run) run, even with nothing to report --
    # notify.telegram sends a one-line "nenhum resultado hoje" heartbeat in
    # that case (found 2026-08-17) instead of leaving the owner unable to
    # tell a quiet day apart from a broken run.
    #
    # Falls back to the known, fixed Pages URL when TERRENO_PAGE_URL isn't
    # set -- found 2026-08-13: the repo variable was apparently never
    # actually set, so "...e mais N — ver a lista completa" had no link at
    # all, a dead end for exactly the listings being pointed at. A repo
    # variable being forgotten should degrade, not silently drop the one
    # thing that line promises.
    # Skipped for a discovery-only run (e.g. --profile ci_novos): those
    # sources never return Listings even on a normal day, so a heartbeat here
    # would just be daily noise on top of the main pipeline's own -- the
    # "did today's run happen" question that heartbeat answers is about the
    # main pipeline, not this bookkeeping job.
    if not set(wanted) <= _SEM_HEALTH_POR_CONTAGEM:
        pagina = env("TERRENO_PAGE_URL") or "https://atismatiz.github.io/Terreno/"
        notify.telegram(para_avisar, pagina,
                        int(criteria.output("top_n_no_alerta", 8)),
                        alertas=alertas_saude)

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
