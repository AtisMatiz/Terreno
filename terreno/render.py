"""Render the static page. One self-contained HTML file, data embedded as JSON,
all sorting and filtering client-side — no build step, no server, works the
same from GitHub Pages, Vercel, or a local file:// open.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("terreno.render")

TEMPLATE = Path(__file__).parent / "templates" / "page.html"

# The page is read in Brazil; stamping it in UTC makes "updated at" needlessly
# hard to read. No tz database dependency: Brazil has had no DST since 2019.
BRT = timezone(timedelta(hours=-3))


def _agora_brasilia() -> str:
    return datetime.now(BRT).strftime("%d/%m/%Y às %H:%M")


def _row_to_json(row) -> dict:
    d = dict(row)
    d["reasons"] = (d.get("reasons") or "").split("\n") if d.get("reasons") else []
    try:
        d["dimensoes"] = json.loads(d.get("dimensoes") or "{}")
    except ValueError:
        d["dimensoes"] = {}
    d.pop("dismissed", None)
    return d


def render(rows, out_dir: Path, *, new_keys: set[str], sources: list[str],
           warnings: list[str] | None = None, criteria=None) -> Path:
    listings = []
    for row in rows:
        item = _row_to_json(row)
        item["is_new"] = item["key"] in new_keys
        if item.get("price") and item.get("price_first"):
            delta = item["price"] - item["price_first"]
            item["price_drop"] = round(delta, 2) if delta else None
        listings.append(item)

    payload = {
        "generated_at": _agora_brasilia(),
        "listings": listings,
        "new_count": sum(1 for i in listings if i["is_new"]),
        "sources": sources,
        "warnings": warnings or [],
        # Pre-fills the "Configurar busca" panel with what this run actually
        # used, so the form always starts from the current state.
        "criterios": {
            "estados": criteria.states,
            "regiao": criteria.regiao,
            "municipios": (criteria.raw.get("localizacao") or {}).get("municipios") or [],
            "area_min": criteria.area_min,
            "area_max": criteria.area_max,
            "preco_min": criteria.price_min,
            "preco_max": criteria.price_max,
        } if criteria else {},
    }

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__",
        json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")

    # A machine-readable copy, so the data is usable without scraping our own page.
    (out_dir / "listings.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log.info("rendered %d listings -> %s", len(listings), out_path)
    return out_path
