"""Parsing and age-checking for `Listing.posted_at`.

Found 2026-08-17 against a real result: a Brave-discovered LinkedIn "pulse"
post from 2018 read as a live sítio listing -- the seller's phrasing was
fine, the property description was fine, the problem was purely that the ad
itself was eight years stale and almost certainly long since sold, taken
down, or re-listed at a different price. A page being freely readable and
grammatically a listing is not evidence it is still an active offer.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# How stale is too stale to trust as a live offer. 4 years, per the user's
# call (2026-08-17) -- already generous for rural land, where a genuine offer
# can sit for a while, but a page this old is far more likely dead than not.
LIMITE_ANOS = 4

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

# "26 de fevereiro de 2018" -- the visible byline date on pages (LinkedIn
# pulse posts, blogs) that carry no machine-readable date at all.
_DATA_PT_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(_MESES_PT) + r")\s+de\s+(\d{4})\b", re.I
)


def parse_data(texto: str) -> datetime | None:
    """Best-effort parse of a listing's stated date, timezone-aware. None
    when nothing recognizable is found -- callers must treat that as
    "unknown", not as "old", same convention as every other optional field
    in this pipeline."""
    texto = (texto or "").strip()
    if not texto:
        return None

    # ISO-8601 first (what facebook.py and rules.py's own JSON-LD/meta
    # reading produce): datetime.fromisoformat wants "+00:00", not "Z".
    iso = texto[:-1] + "+00:00" if texto.endswith("Z") else texto
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    m = _DATA_PT_RE.search(texto)
    if m:
        dia, mes_nome, ano = m.groups()
        mes = _MESES_PT[mes_nome.lower()]
        try:
            return datetime(int(ano), mes, int(dia), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def encontrar_data_pt(texto: str) -> str:
    """Raw matched substring for the "DD de MES de YYYY" pattern anywhere in
    `texto`, or "" -- for callers (rules.py) that need the actual snippet to
    store as `posted_at`, not just a yes/no."""
    m = _DATA_PT_RE.search(texto or "")
    return m.group(0) if m else ""


def muito_antigo(texto: str, limite_anos: float = LIMITE_ANOS) -> bool:
    """True only when a date was actually found AND it is older than
    `limite_anos`. No date at all is not "old" -- it is unknown, and an
    unknown-dated listing (most of them; few portals expose a real
    publication date) must keep passing through same as it always has."""
    dt = parse_data(texto)
    if dt is None:
        return False
    idade_dias = (datetime.now(timezone.utc) - dt).days
    return idade_dias > limite_anos * 365.25
