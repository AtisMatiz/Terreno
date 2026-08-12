"""Parsing of Brazilian area and price strings into canonical units.

Everything in the pipeline works in hectares and BRL. This module is the only
place that knows how to get there from the text a portal actually prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hectares per unit. The alqueire is regional and genuinely ambiguous: the
# paulista is 2.42 ha, the mineiro/geral 4.84 ha. When a listing just says
# "alqueire" we resolve it by the state the listing is in (see area_to_ha).
_ALQUEIRE_BY_UF = {
    "SP": 2.42, "PR": 2.42, "RJ": 2.42, "GO": 4.84, "MG": 4.84, "BA": 4.84,
    "MT": 3.3856, "MS": 3.3856, "PA": 3.3856, "AM": 2.72, "RO": 3.3856,
}

# The name of the variant each factor is, so a card can say "3 alqueires
# (paulista)" instead of only a hectare number nobody can check.
_ALQUEIRE_TIPO = {
    2.42: "paulista",
    4.84: "mineiro",
    3.3856: "norte",
    2.72: "amazonense",
}

# There is deliberately NO default factor. Guessing one is what this module
# used to do (4.84, "alqueire geral"), and it is wrong by exactly 2x across
# São Paulo -- the whole search region -- so a listing whose UF had not been
# resolved yet had its area doubled, and with it R$/ha and the area filter.
# When the variant cannot be established, the alqueire figure is reported
# unconverted (see area_detalhada) and the hectare value stays None.

_UNIT_HA = {
    "ha": 1.0, "hectare": 1.0, "hectares": 1.0, "hec": 1.0, "há": 1.0,
    "m2": 1e-4, "m²": 1e-4, "metros": 1e-4, "metro": 1e-4, "m": 1e-4,
    "km2": 100.0, "km²": 100.0,
    "tarefa": 0.3025,      # Bahia/Nordeste
}

_NUM = r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)"


def parse_number(raw: str) -> float | None:
    """Parse a Brazilian-formatted number. '1.234,56' -> 1234.56."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    if "," in s:
        # Comma is the decimal separator; dots are thousands.
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1:
        head, tail = s.split(".")
        # A lone dot with exactly three trailing digits is a thousands
        # separator ("1.500"), not a decimal point.
        if len(tail) == 3 and len(head) <= 3:
            s = head + tail
    else:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class AreaDetalhada:
    """An area as it can honestly be reported.

    `ha` is None whenever no hectare figure can be established *without
    guessing*; `alqueires` carries the raw alqueire count in that case so the
    card can print "3 alqueires" instead of a fabricated hectare number.
    `alqueire_tipo` is the variant the conversion used, or "" when unknown.
    """

    ha: float | None = None
    alqueires: float | None = None
    alqueire_tipo: str = ""


def _por_unidade(text: str, *, apenas_grandes: bool = False) -> float | None:
    """First non-alqueire area in `text`, in hectares.

    `apenas_grandes` keeps only units a rural plot is actually measured in
    (ha, km²). It is used when the text already gave an alqueire figure: a
    page that prints "3 alqueires (7,26 ha)" is doing the conversion for us
    and is worth trusting, while the m² figure on the same page is almost
    always the built area of the house, not the land.
    """
    # Longest unit names first so "hectares" is not shadowed by "ha".
    for unit in sorted(_UNIT_HA, key=len, reverse=True):
        if apenas_grandes and _UNIT_HA[unit] < 1.0:
            continue
        # Allow the plural: listings say "3 tarefas" as often as "1 tarefa".
        plural = "s?" if unit[-1].isalpha() else ""
        pattern = _NUM + r"\s*" + re.escape(unit) + plural + r"\b"
        m = re.search(pattern, text)
        if m:
            value = parse_number(m.group(1))
            if value is not None:
                return round(value * _UNIT_HA[unit], 4)
    return None


def area_detalhada(raw: str | None, uf: str | None = None) -> AreaDetalhada:
    """Extract an area from free text, keeping the alqueire visible.

    Same reading rules as before for every unit except the alqueire. For the
    alqueire, the variant is resolved from the UF; when the UF is empty or not
    one whose variant we know, nothing is converted — the alqueire count is
    returned as-is and `ha` stays None unless the text itself also states a
    hectare (or km²) figure.
    """
    if not raw:
        return AreaDetalhada()
    text = str(raw).lower().replace("\xa0", " ")

    m = re.search(_NUM + r"\s*(alqueire?s?)", text)
    if m:
        value = parse_number(m.group(1))
        if value is not None:
            factor = _ALQUEIRE_BY_UF.get((uf or "").upper())
            if factor is not None:
                return AreaDetalhada(
                    ha=round(value * factor, 4),
                    alqueires=value,
                    alqueire_tipo=_ALQUEIRE_TIPO.get(factor, ""),
                )
            # Ambiguous alqueire: no guess. Accept a hectare figure the page
            # itself states, if any, and otherwise report the alqueire alone.
            return AreaDetalhada(
                ha=_por_unidade(text, apenas_grandes=True),
                alqueires=value,
                alqueire_tipo="",
            )

    return AreaDetalhada(ha=_por_unidade(text))


def area_to_ha(raw: str | None, uf: str | None = None) -> float | None:
    """Extract an area from free text and convert to hectares.

    Returns None when no area can be read — callers treat that as unknown, not
    as zero, so an unparseable listing is surfaced rather than silently filtered
    out by an area floor. An alqueire whose variant cannot be determined is one
    such unknown: see `area_detalhada`, which also hands back the raw alqueire.
    """
    return area_detalhada(raw, uf).ha


def price_to_brl(raw: str | None) -> float | None:
    """Extract a BRL price from free text.

    Rejects obvious non-prices (zero, or the 'sob consulta' placeholder) by
    returning None so they are not mistaken for a bargain.
    """
    if raw is None:
        return None
    text = str(raw).lower()
    if "consulta" in text or "combinar" in text:
        return None

    m = re.search(r"r\$\s*" + _NUM, text)
    if m:
        value = parse_number(m.group(1))
    elif len(text) <= 40:
        # Only trust a bare number when the string is short enough to plausibly
        # be a price field. In long prose, "17.023 Terrenos à venda" would
        # otherwise be read as a price of R$ 17.023.
        value = parse_number(text)
    else:
        return None
    if value is None or value <= 0:
        return None

    # A price expressed in millions ("R$ 1,2 milhão").
    if re.search(r"milh(ão|ões|oes)", text) and value < 1000:
        value *= 1_000_000
    elif re.search(r"\bmil\b", text) and value < 1000:
        value *= 1_000
    return round(value, 2)


def price_per_ha(price: float | None, area_ha: float | None) -> float | None:
    if not price or not area_ha or area_ha <= 0:
        return None
    return round(price / area_ha, 2)
