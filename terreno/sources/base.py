"""Shared helpers for Layer A portal scrapers.

Every source exposes the same callable shape:

    def fetch(criteria, store, budgets) -> list[Listing]

and is expected never to raise: a portal that changes its markup, walls us off,
or simply goes down must degrade to an empty list with a warning, not take the
run with it. `run_source` in terreno/run.py enforces that with a try/except, but
sources should still fail softly on their own.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator

log = logging.getLogger("terreno.sources")

UF_NAMES = {
    "AC": "acre", "AL": "alagoas", "AP": "amapa", "AM": "amazonas",
    "BA": "bahia", "CE": "ceara", "DF": "distrito-federal", "ES": "espirito-santo",
    "GO": "goias", "MA": "maranhao", "MT": "mato-grosso", "MS": "mato-grosso-do-sul",
    "MG": "minas-gerais", "PA": "para", "PB": "paraiba", "PR": "parana",
    "PE": "pernambuco", "PI": "piaui", "RJ": "rio-de-janeiro",
    "RN": "rio-grande-do-norte", "RS": "rio-grande-do-sul", "RO": "rondonia",
    "RR": "roraima", "SC": "santa-catarina", "SP": "sao-paulo",
    "SE": "sergipe", "TO": "tocantins",
}


def next_data(html: str) -> dict | None:
    """Pull the __NEXT_DATA__ blob out of a Next.js page.

    Several Brazilian portals are Next.js apps that ship their full, already
    structured listing payload in this script tag. Reading it is dramatically
    more robust than parsing the rendered cards — the JSON shape changes far
    less often than the CSS classes do.
    """
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def walk(node, key: str) -> Iterator:
    """Yield every value stored under `key` anywhere in a nested structure.

    Lets a source say "give me every object that has an 'listingId'" without
    hard-coding the exact path, which is the part that breaks on redesigns.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, key)


def json_ld(html: str) -> list[dict]:
    """Every application/ld+json object on the page, flattened."""
    out: list[dict] = []
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
            if isinstance(data.get("@graph"), list):
                out.extend(d for d in data["@graph"] if isinstance(d, dict))
    return out


def strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"\s+", " ", text).strip()
