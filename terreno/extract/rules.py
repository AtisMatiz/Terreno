"""Rules-based extraction of a listing from an arbitrary page.

This is the default extractor for Layer B and costs nothing. It tries, in
order: JSON-LD (best), OpenGraph/meta tags, then the visible text. The LLM
extractor is only consulted when this returns something too thin to use.
"""

from __future__ import annotations

import re
import unicodedata

from ..models import Listing
from ..sources.base import json_ld, strip_tags
from ..units import area_to_ha, price_to_brl

_META = r'<meta[^>]+(?:property|name)=["\']{key}["\'][^>]+content=["\']([^"\']*)["\']'

# Municipality/UF as it is normally written in a Brazilian listing.
_LOCATION_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç'’\-]+"
    r"(?:\s+(?:de|do|da|dos|das)\s+[\w\-áéíóúâêôãõç]+|\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç\-]+)*)"
    r"\s*[-/,]\s*(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b"
)


def meta(html: str, key: str) -> str:
    m = re.search(_META.format(key=re.escape(key)), html, re.I)
    return m.group(1).strip() if m else ""


def extract(html: str, url: str, source: str = "brave") -> Listing | None:
    """Best-effort structured listing from a page. None when the page clearly
    is not an individual offer."""
    title = ""
    description = ""
    price = None
    area_ha = None
    image = ""
    municipality = ""
    uf = ""

    listing_nodes = []
    for node in json_ld(html):
        types = node.get("@type") or ""
        types = types if isinstance(types, list) else [types]
        if any(t in ("Product", "Offer", "RealEstateListing", "Residence",
                     "Place", "Apartment", "House") for t in types):
            listing_nodes.append(node)

    # More than one offer/listing node in the page's own structured data means
    # this is a category or search-results page listing several properties,
    # not a single one -- there is no honest way to say which node "belongs"
    # to this URL, so treat it the same as no listing at all rather than
    # silently picking the first one.
    if len(listing_nodes) > 1:
        return None

    for node in listing_nodes:
        title = title or node.get("name") or ""
        description = description or node.get("description") or ""
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not price and offers.get("price"):
            price = price_to_brl(str(offers["price"]))
        address = node.get("address") or {}
        if isinstance(address, dict):
            municipality = municipality or address.get("addressLocality") or ""
            uf = uf or (address.get("addressRegion") or "")[:2].upper()

    title = title or meta(html, "og:title") or _title_tag(html)
    description = description or meta(html, "og:description")
    image = meta(html, "og:image")

    body = strip_tags(html)[:6000]
    haystack = f"{title} {description} {body}"

    preco_estruturado = price is not None
    if price is None:
        price = price_to_brl(haystack)
    if area_ha is None:
        area_ha = area_to_ha(haystack, uf)

    # A page mentioning several distinct "R$ ..." amounts is very likely a
    # category or search-results page listing many properties, not a single
    # offer -- the price we just read above is only whichever one happened to
    # come first in the text, not necessarily the one this URL is "about".
    # Structured data is trusted regardless, since it names a specific offer
    # even on a page whose surrounding text also mentions other prices (e.g.
    # a "similar listings" sidebar).
    if not preco_estruturado and len(re.findall(r"r\$\s*\d", haystack, re.I)) >= 3:
        return None

    if not municipality:
        m = _LOCATION_RE.search(f"{title} {description}") or _LOCATION_RE.search(body)
        if m:
            municipality, uf = _clean_municipality(m.group(1)), m.group(2)

    # A page with neither a price nor an area is almost certainly a category
    # or index page rather than a single offer — not worth a card.
    if price is None and area_ha is None:
        return None

    return Listing(
        source=source,
        url=url,
        title=title[:300],
        description=(description or body)[:2000],
        price=price,
        area_ha=area_ha,
        municipality=municipality,
        uf=uf,
        image=image,
    )


# Words that can precede a municipality in listing prose but are never part of
# its name. "FAZENDA RICA EM ÁGUA EM DATAS/MG" must yield "Datas", not the
# whole phrase.
_STOP = {
    "em", "no", "na", "de", "do", "da", "dos", "das", "para", "com", "sem",
    "regiao", "região", "cidade", "zona", "rural", "proximo", "próximo",
    "fazenda", "sitio", "sítio", "chacara", "chácara", "terreno", "area", "área",
    "venda", "vendo", "rica", "otima", "ótima", "linda", "excelente",
    # Legal/registry boilerplate that sits next to a UF in page footers.
    "hipotecas", "comarca", "cartorio", "cartório", "registro", "matricula",
    "matrícula", "oficio", "ofício", "tabelionato", "imoveis", "imóveis",
}


def _fold_word(word: str) -> str:
    """Lowercase, strip accents and punctuation, for stop-word comparison."""
    folded = unicodedata.normalize("NFKD", word.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return folded.strip(",.-()")


def _clean_municipality(raw: str) -> str:
    """Trim listing prose down to the municipality name.

    Returns "" rather than a guess when the phrase cannot be trimmed
    confidently — an empty municipality is honest and merely disables the
    radius filter for that listing, while a wrong one silently misplaces it.
    """
    text = raw.strip()
    # "Fazenda rica em água em Datas" — the municipality follows the *last*
    # locative connector, not the first.
    parts = re.split(r"(?i)(?<!\w)(?:em|no|na|próximo a|proximo a|perto de)(?!\w)", text)
    if len(parts) > 1 and parts[-1].strip():
        text = parts[-1]

    words = [w for w in re.split(r"\s+", text.strip()) if w]
    while words and _fold_word(words[0]) in _STOP:
        words.pop(0)
    # Municipality names run to about four words ("Pedras de Maria da Cruz" is
    # five, so allow six before giving up).
    if not words or len(words) > 6:
        return ""
    name = " ".join(words)
    # Normalize shouty listings, keeping the small connectives lowercase.
    if name.isupper():
        name = " ".join(
            w.capitalize() if w.lower() not in {"de", "do", "da", "dos", "das"}
            else w.lower()
            for w in name.split()
        )
    return name


def is_thin(listing: Listing | None) -> bool:
    """Whether the LLM extractor is worth spending a call on."""
    if listing is None:
        return True
    missing = sum(1 for v in (listing.price, listing.area_ha) if v is None)
    return missing >= 1 or not listing.municipality


def _title_tag(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return strip_tags(m.group(1)) if m else ""
