"""Classifies an SDB (`sites_descobertos`) host as an imobiliária (real-estate
agency/broker site) or "outro" (anything else -- blogs, city-hall pages,
forums, syndicates) -- so each category can be scanned by a different
strategy: imobiliárias have a predictable "listings for sale" structure worth
crawling directly (see `sources/imobiliaria_crawl.py`); everything else stays
on search-API discovery (Brave/Tavily), where there is no such structure to
exploit.

Kept deliberately simple and code-only, not an LLM call: the two signals below
are cheap, deterministic, and -- for CRECI in particular -- close to
ground-truth, because Brazilian law requires a licensed real-estate broker to
display their CRECI registration number. A wrong classification here silently
routes a host to the wrong scanning strategy for weeks (the weekly `site:`
rotation), so precision matters more than recall: when neither signal fires,
this returns "outro" rather than guessing.
"""

from __future__ import annotations

import re

IMOBILIARIA = "imobiliaria"
OUTRO = "outro"

# Substrings, not whole words: domains are compound
# ("betoimoveisfazendas.com.br", "hsimoveisrurais.com.br") and a substring
# match on the unhyphenated hostname is the only way to catch that.
_HOST_HINTS = ("imov", "imobil", "corretor", "realty", "realestate")

# CRECI (Conselho Regional de Corretores de Imóveis) is the state broker
# registry number every licensed Brazilian real-estate professional or
# agency is legally required to publish -- seeing it on a page is about as
# close to a certain "this is a real-estate business" signal as free text
# gets. Word boundary so it doesn't fire on an unrelated word containing the
# same letters.
_CRECI_RE = re.compile(r"\bcreci\b", re.I)


def classificar(host: str, texto: str = "") -> str:
    """`host` is required; `texto` is whatever free text is available at
    classification time (a Brave snippet title+description, or a full page
    body) -- the more text, the better the odds of catching a CRECI mention,
    but an empty string is a safe, ordinary call (falls back to the host
    check alone)."""
    if texto and _CRECI_RE.search(texto):
        return IMOBILIARIA
    host_l = (host or "").lower()
    if any(hint in host_l for hint in _HOST_HINTS):
        return IMOBILIARIA
    return OUTRO
