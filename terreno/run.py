"""Entry point. One invocation does the whole run:

    python -m terreno.run [--only SOURCE] [--dry-run]

Every source is isolated: one portal blocking or changing its markup costs that
source's listings and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import http, notify, pipeline, render
from .config import (DB_PATH, SITE_DIR, env, llm_enabled, load_criteria,
                     salvar_criterios)
from .sources import REGISTRY
from .store import Store

log = logging.getLogger("terreno")


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
    for name in wanted:
        fetch = REGISTRY.get(name)
        if not fetch:
            warnings.append(f"{name}: unknown source")
            continue
        found, error = run_source(name, fetch, criteria, store, budgets)
        if error:
            warnings.append(error)
        elif not found:
            warnings.append(f"{name}: 0 resultados")
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

    if fresh:
        notify.telegram(fresh, env("TERRENO_PAGE_URL"),
                        int(criteria.output("top_n_no_alerta", 8)))

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
