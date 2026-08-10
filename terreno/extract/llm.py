"""Optional LLM extraction and fit scoring.

Off unless ENABLE_LLM=1 and ANTHROPIC_API_KEY is set. It is consulted only for
pages the free rules-based extractor could not read, so the token bill scales
with how bad the markup is, not with how many listings exist.
"""

from __future__ import annotations

import json
import logging
import os

from ..models import Listing
from ..sources.base import strip_tags

log = logging.getLogger("terreno.extract.llm")

MODEL = os.getenv("TERRENO_MODEL", "claude-haiku-4-5-20251001")
MAX_CHARS = 6000

_SCHEMA = {
    "name": "listing",
    "description": "Structured land-for-sale listing extracted from a web page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_land_for_sale": {"type": "boolean"},
            "title": {"type": "string"},
            "price_brl": {"type": ["number", "null"]},
            "area_ha": {"type": ["number", "null"]},
            "municipality": {"type": "string"},
            "uf": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["is_land_for_sale"],
    },
}

_PROMPT = (
    "Extract the land-for-sale details from this Brazilian web page. "
    "Convert any area to hectares (1 alqueire paulista = 2.42 ha, "
    "1 alqueire mineiro/geral = 4.84 ha, 1 tarefa = 0.3025 ha, "
    "10000 m² = 1 ha). Prices are in BRL. If the page is a search results or "
    "category page rather than one specific plot, set is_land_for_sale=false. "
    "Never guess a price or an area that is not stated — return null instead.\n\n"
)


def _client():
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — LLM extraction unavailable")
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def extract(html: str, url: str, source: str = "brave") -> Listing | None:
    """Extract a listing with the model. Returns None on any failure — the
    caller keeps whatever the rules extractor produced."""
    client = _client()
    if client is None:
        return None

    text = strip_tags(html)[:MAX_CHARS]
    if len(text) < 200:
        return None

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=800,
            tools=[_SCHEMA],
            tool_choice={"type": "tool", "name": "listing"},
            messages=[{"role": "user", "content": _PROMPT + text}],
        )
    except Exception as exc:  # noqa: BLE001 — never fail the run on the optional path
        log.warning("llm extract failed for %s: %s", url, exc)
        return None

    data = None
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            data = block.input
            break
    if not data or not data.get("is_land_for_sale"):
        return None

    return Listing(
        source=source,
        url=url,
        title=(data.get("title") or "")[:300],
        description=(data.get("summary") or "")[:2000],
        price=data.get("price_brl"),
        area_ha=data.get("area_ha"),
        municipality=data.get("municipality") or "",
        uf=(data.get("uf") or "")[:2].upper(),
    )


def fit_score(listing: Listing, must: list[str], nice: list[str]) -> tuple[float, str] | None:
    """Nuanced 0..1 fit score against the prose criteria. Optional; the
    deterministic scorer already ran and stays authoritative if this fails."""
    client = _client()
    if client is None:
        return None

    prompt = (
        f"Land listing:\nTitle: {listing.title}\nDescription: {listing.description[:1500]}\n"
        f"Price: {listing.price}\nArea (ha): {listing.area_ha}\n"
        f"Location: {listing.municipality}/{listing.uf}\n\n"
        f"Buyer requires: {', '.join(must)}\n"
        f"Buyer would like: {', '.join(nice)}\n\n"
        "Reply with only a JSON object: "
        '{"score": <0..1>, "note": "<one short sentence in Portuguese>"}'
    )
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return float(data["score"]), str(data.get("note", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("llm score failed for %s: %s", listing.url, exc)
        return None
