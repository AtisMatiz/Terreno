"""Rules-based matching of the qualities named in criteria.yaml.

This is the free, deterministic scorer. It runs always. The optional LLM scorer
(terreno/extract/llm.py) layers on top when ENABLE_LLM=1 — it never replaces
this, so turning the key off degrades quality rather than breaking the run.
"""

from __future__ import annotations

import re
import unicodedata

# Each quality maps to Portuguese surface forms as they actually appear in
# Brazilian rural listings. Matching is accent-insensitive and word-bounded.
SYNONYMS: dict[str, list[str]] = {
    "agua": [
        "agua", "rio", "riacho", "corrego", "córrego", "nascente", "mina dagua",
        "mina d agua", "acude", "represa", "lagoa", "poco artesiano", "poco",
        "cachoeira", "olho dagua", "brejo",
    ],
    "acesso": [
        "acesso", "estrada", "asfalto", "beira de pista", "rodovia", "br-",
        "acesso facil", "carro de passeio", "trafegavel",
    ],
    "matricula": [
        "matricula", "escritura", "registrado", "documentacao em dia",
        "documento ok", "car ", "georreferenciado", "titulo definitivo",
    ],
    "mata": [
        "mata", "mata nativa", "floresta", "cerrado", "reserva legal",
        "vegetacao nativa", "araucaria", "capoeira", "app",
    ],
    "energia": [
        "energia", "luz", "rede eletrica", "trifasica", "monofasica",
        "energia eletrica", "cemig", "coelba",
    ],
    "benfeitorias": [
        "benfeitoria", "casa", "sede", "curral", "galpao", "cercado", "cercada",
        "barracao", "paiol", "pastagem formada",
    ],
    "posse": [
        "posse", "sem escritura", "sem matricula", "documentacao irregular",
        "so contrato", "contrato de gaveta", "nao registrado",
    ],
    "invasao": ["invasao", "invadido", "ocupacao", "sem terra", "mst"],
    "litigio": [
        "litigio", "inventario", "espolio", "judicial", "penhora", "usucapiao",
        "acao judicial",
    ],
}


def _fold(text: str) -> str:
    """Lowercase and strip accents so 'córrego' matches 'corrego'."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def detect(text: str, quality: str) -> bool:
    """True when `quality` appears in `text`.

    Negations are respected: 'sem energia' and 'não possui água' must not count
    as a positive match, which is exactly the mistake a naive substring search
    makes on real listings.
    """
    folded = _fold(text)
    for term in SYNONYMS.get(quality, [quality]):
        term_f = _fold(term).strip()
        if not term_f:
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(term_f), folded):
            window = folded[max(0, m.start() - 30):m.start()]
            if re.search(r"\b(sem|nao|não|nenhum[a]?|falta de|ausencia de)\s+[\w\s]{0,20}$", window):
                continue
            return True
    return False


def score(listing_text: str, must: list[str], nice: list[str],
          breakers: list[str]) -> tuple[float, list[str], bool]:
    """Return (score 0..1, human-readable reasons, disqualified).

    A deal-breaker disqualifies outright. Missing must-haves cost heavily but do
    not disqualify: rural listings are terse, and absence of the word "água" is
    weak evidence of absence of water.
    """
    reasons: list[str] = []

    for b in breakers:
        if detect(listing_text, b):
            return 0.0, [f"⛔ {b}"], True

    must_hits = 0
    for q in must:
        if detect(listing_text, q):
            must_hits += 1
            reasons.append(f"✓ {q}")
        else:
            reasons.append(f"? {q} (não mencionado)")

    nice_hits = 0
    for q in nice:
        if detect(listing_text, q):
            nice_hits += 1
            reasons.append(f"+ {q}")

    must_part = (must_hits / len(must)) if must else 1.0
    nice_part = (nice_hits / len(nice)) if nice else 0.0
    return round(0.7 * must_part + 0.3 * nice_part, 3), reasons, False
