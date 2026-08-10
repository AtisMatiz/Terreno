"""Weighted scoring of a listing against the fixed buyer profile.

Replaces the earlier flat must/nice/deal-breaker lists. The criteria this
encodes are structural — they describe the kind of property being looked for,
not a per-run setting — so they live in code, are versioned, and are testable.
Only size, price and location change per run (criteria.yaml).

Every dimension returns a 0..1 sub-score plus the evidence that produced it, so
a card can show *why* it ranked where it did rather than an opaque number.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- gate

# The property must read as rural land with a homestead. A bare urban lot, a
# house in town, or a plot in a gated development is not what is wanted.
TIPO_RURAL = r"fazenda|chacara|sitio|haras|rancho|area rural|zona rural|propriedade rural|gleba"
TIPO_URBANO = r"loteamento|condominio fechado|lote urbano|terreno urbano|apartamento|sobrado|casa geminada"


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _hits(text: str, pattern: str) -> int:
    """Occurrences of `pattern`, ignoring negated mentions.

    "sem nascente" and "não possui água" must not read as positives — the
    single most common way a naive keyword scorer inflates a bad listing.
    """
    count = 0
    for m in re.finditer(pattern, text):
        before = text[max(0, m.start() - 28):m.start()]
        if re.search(r"\b(sem|nao|nenhum[a]?|falta de|ausencia de|nem)\s+[\w\s]{0,18}$", before):
            continue
        count += 1
    return count


# ---------------------------------------------------------------- dimensions
# (pattern, weight, label). Weights inside a dimension are summed and clipped
# to 1.0; negatives subtract. Labels are what the page shows.

DIMENSOES: dict[str, dict] = {
    "agua": {
        "peso": 30,          # by far the most important criterion
        "rotulo": "Água",
        "positivos": [
            (r"mina d.?agua|mina de agua", 0.55, "mina d'água"),
            (r"nascentes?", 0.60, "nascente"),
            (r"cachoeiras?", 0.35, "cachoeira"),
            (r"\brios?\b|riachos?|corregos?|ribeiroes?|ribeirao", 0.40, "rio/riacho"),
            (r"lagoas?|\blagos?\b|represas?|acudes?", 0.25, "lago/represa"),
            (r"pocos? artesianos?|pocos? semi.?artesianos?", 0.20, "poço artesiano"),
            (r"rica? em agua|abundancia de agua|muita agua|agua abundante", 0.45, "água abundante"),
            (r"agua de nascente|agua pura|agua cristalina", 0.25, "água de qualidade"),
        ],
        "negativos": [
            (r"sem agua|falta de agua|problema de agua|agua escassa", 1.0, "pouca água"),
        ],
        # More than one nascente is materially better than one.
        "contagem": (r"(duas|tres|quatro|varias|\b[2-9])\s+nascentes", 0.30, "várias nascentes"),
    },
    "benfeitorias": {
        "peso": 15,
        "rotulo": "Benfeitorias",
        "positivos": [
            (r"casa sede|sede da fazenda|casa principal", 0.50, "casa sede"),
            (r"casa (?:do |de )?caseiro|casa de colono", 0.35, "casa de caseiro"),
            (r"(duas|tres|\b[2-9])\s+casas", 0.55, "mais de uma casa"),
            (r"\bcasas?\b|residencia|moradia", 0.35, "casa"),
            (r"currais?|estabulo|mangueira", 0.12, "curral"),
            (r"galpoes?|galpao|barracao|paiol", 0.12, "galpão"),
            (r"energia|luz eletrica|rede eletrica|trifasic|monofasic", 0.18, "energia"),
            (r"cercad[oa]|cercas?\b", 0.08, "cercado"),
            (r"piscinas?", 0.05, "piscina"),
        ],
        "negativos": [
            (r"sem benfeitorias|sem construcao|terreno limpo|nenhuma construcao", 1.0,
             "sem benfeitorias"),
        ],
    },
    "silencio": {
        "peso": 10,
        "rotulo": "Sossego",
        "positivos": [
            (r"sossegad[oa]|sossego|muito silencios[oa]|bem silencios[oa]", 0.50, "sossegado"),
            (r"sem vizinhos|nenhum vizinho|sem vizinhanca|privacidade total", 0.50, "sem vizinhos"),
            (r"tranquil[oa]|tranquilidade|paz|paraiso|refugio", 0.28, "tranquilo"),
            (r"isolad[oa]|reservad[oa]|recolhid[oa]|no fim da estrada", 0.30, "reservado"),
            (r"longe da cidade|longe do centro|longe de tudo", 0.20, "longe da cidade"),
        ],
        "negativos": [
            (r"beira da rodovia|pe na pista|de frente para a rodovia|as margens da br",
             0.55, "na beira da rodovia"),
            (r"condominio|loteamento|vizinhos proximos", 0.45, "vizinhança próxima"),
            (r"proximo ao centro|no centro|area urbana", 0.35, "perto do centro"),
        ],
    },
    "acessibilidade": {
        "peso": 10,
        "rotulo": "Acesso",
        "positivos": [
            (r"asfalto ate|acesso asfaltad[oa]|totalmente asfaltad[oa]", 0.50, "asfalto"),
            (r"acesso (?:por |de )?(?:qualquer )?carro|carro de passeio|carro comum",
             0.45, "acesso por carro comum"),
            (r"bom acesso|otimo acesso|facil acesso|acesso facil|bem acessivel",
             0.35, "bom acesso"),
            (r"estrada (?:boa|conservada|bem conservada)", 0.25, "estrada conservada"),
        ],
        "negativos": [
            (r"(?:somente|so|apenas) (?:de |com )?4x4|precisa de 4x4|traca[oa] nas 4",
             1.0, "só 4x4"),
            (r"dificil acesso|acesso dificil|estrada ruim|estrada precaria",
             0.70, "acesso difícil"),
        ],
        # "3 km de estrada de terra" — parsed, not guessed. See _dist_terra.
        "medida": "estrada_terra",
    },
    "distancia": {
        "peso": 5,
        "rotulo": "Distância",
        "positivos": [
            (r"proxim[oa] a cidade|perto da cidade|proxim[oa] ao comercio|minutos do centro",
             0.30, "perto da cidade"),
        ],
        "negativos": [],
        "medida": "distancia_cidade",
    },
    "topografia": {
        "peso": 10,
        "rotulo": "Topografia",
        "positivos": [
            (r"\bplan[oa]s?\b|terreno plano|area plana|totalmente plan[oa]", 0.50, "plano"),
            (r"levemente ondulad[oa]|pouca declividade|suave ondulad[oa]|baixa declividade",
             0.40, "pouca declividade"),
            (r"boa topografia|topografia excelente|topografia favoravel", 0.30, "boa topografia"),
        ],
        "negativos": [
            (r"muito acidentad[oa]|declive acentuad[oa]|ingreme|montanhos[oa]|so serra",
             0.65, "acidentado"),
            (r"\bacidentad[oa]\b", 0.40, "acidentado"),
        ],
        "medida": "percentual_plano",
    },
    "fertilidade": {
        "peso": 10,
        "rotulo": "Solo",
        "positivos": [
            (r"terra (?:boa|otima|forte|fertil|roxa|vermelha)|solo fertil|terra de cultura",
             0.55, "terra boa"),
            (r"agrofloresta|sistema agroflorestal|\bsaf\b|agroecolog|organic[oa]|permacultura",
             0.55, "manejo regenerativo"),
            (r"sem agrotoxico|livre de agrotoxico|nunca (?:usou|utilizou) veneno",
             0.55, "sem agrotóxico"),
            (r"pastage[nm]s?|pasto formad[oa]|braquiaria", 0.18, "pastagem"),
            (r"frutiferas?|pomar", 0.20, "frutíferas"),
        ],
        "negativos": [
            (r"degradad[oa]|erosao|voçoroca|vocoroca|terra fraca|solo pobre|esgotad[oa]",
             0.80, "degradado"),
            (r"agrotoxic|veneno|pulverizac", 0.50, "histórico de agrotóxico"),
            (r"monocultura|\bsoja\b|\bcana\b|canavial|algodao", 0.45, "monocultura"),
            (r"eucalipt|pinus", 0.25, "eucalipto/pinus"),
        ],
    },
    "mata": {
        "peso": 10,
        "rotulo": "Mata nativa",
        "positivos": [
            (r"mata nativa|floresta nativa|vegetacao nativa|mata virgem", 0.60, "mata nativa"),
            (r"mata atlantica|cerrado|araucaria|caatinga preservada", 0.35, "bioma preservado"),
            (r"reserva legal|\brl\b|area de preservacao|\bapp\b", 0.30, "reserva legal"),
            (r"\bmatas?\b|\bmatinha\b|capoeira|bosque", 0.25, "mata"),
            (r"nativas? preservad|bem preservad|muito verde", 0.25, "preservado"),
        ],
        "negativos": [
            (r"totalmente desmatad|sem mata|sem vegetacao|tudo limpo", 0.70, "desmatado"),
        ],
    },
    "regularizacao": {
        "peso": 10,
        "rotulo": "Documentação",
        "positivos": [
            (r"escritur|matricula (?:propria|registrada|individual)|registrad[oa] em cartorio",
             0.60, "escriturado"),
            (r"documentacao em dia|documentos? ok|documentacao regular", 0.35, "documentação em dia"),
            (r"georreferenciad|\bccir\b|\bcar\b|\bincra\b|itr em dia", 0.25, "georreferenciado"),
        ],
        "negativos": [
            (r"usucapiao", 0.60, "usucapião"),
            (r"sem escritura|sem matricula|apenas posse|so posse|contrato de gaveta|documentacao irregular",
             0.80, "sem escritura"),
            (r"inventario|espolio|litigio|penhora|acao judicial", 0.50, "pendência judicial"),
        ],
    },
}

PESO_TOTAL = sum(d["peso"] for d in DIMENSOES.values())


# ---------------------------------------------------------------- measures
def _dist_terra(text: str) -> tuple[float | None, str]:
    """Kilometres of dirt road, when the listing states it.

    Preference: under 2 km ideal, up to 4-5 km acceptable, beyond that a real
    penalty.
    """
    m = re.search(r"([\d.,]+)\s*(?:km|quilometros?)\s+(?:de\s+)?(?:estrada\s+de\s+)?"
                  r"(?:terra|chao|nao pavimentad)", text)
    if not m:
        m = re.search(r"(?:estrada\s+de\s+)?(?:terra|chao)[^.]{0,20}?([\d.,]+)\s*km", text)
    if not m:
        return None, ""
    try:
        km = float(m.group(1).replace(",", "."))
    except ValueError:
        return None, ""
    if km <= 2:
        return 0.45, f"{km:g} km de terra"
    if km <= 5:
        return 0.15, f"{km:g} km de terra"
    return -0.60, f"{km:g} km de terra"


def _dist_cidade(text: str) -> tuple[float | None, str]:
    """Minutes or kilometres to town. The target is 18 minutes or less."""
    m = re.search(r"([\d.,]+)\s*(?:min|minutos?)\s+(?:d[aeo]s?\s+)?"
                  r"(?:cidade|centro|comercio|supermercado|asfalto)", text)
    if m:
        try:
            minutos = float(m.group(1).replace(",", "."))
        except ValueError:
            return None, ""
        if minutos <= 18:
            return 0.70, f"{minutos:g} min da cidade"
        if minutos <= 30:
            return 0.10, f"{minutos:g} min da cidade"
        return -0.50, f"{minutos:g} min da cidade"

    m = re.search(r"([\d.,]+)\s*(?:km|quilometros?)\s+(?:d[aeo]s?\s+)?(?:cidade|centro)", text)
    if m:
        try:
            km = float(m.group(1).replace(",", "."))
        except ValueError:
            return None, ""
        # Roughly 40 km/h on the mixed roads involved: 18 min ≈ 12 km.
        if km <= 12:
            return 0.70, f"{km:g} km da cidade"
        if km <= 25:
            return 0.10, f"{km:g} km da cidade"
        return -0.50, f"{km:g} km da cidade"
    return None, ""


def _percentual_plano(text: str) -> tuple[float | None, str]:
    """"60% plano" / "70% de área plana" — the share of usable flat land."""
    m = re.search(r"([\d]{1,3})\s*%\s*(?:de\s+)?(?:area\s+)?"
                  r"(?:plan|mecaniza|agricultavel|aproveitav)", text)
    if not m:
        return None, ""
    pct = int(m.group(1))
    if pct > 100:
        return None, ""
    if pct >= 50:
        return 0.55, f"{pct}% plano"
    if pct >= 30:
        return 0.20, f"{pct}% plano"
    return -0.20, f"{pct}% plano"


MEDIDAS = {
    "estrada_terra": _dist_terra,
    "distancia_cidade": _dist_cidade,
    "percentual_plano": _percentual_plano,
}


# ---------------------------------------------------------------- scoring
def tipo_ok(text: str) -> tuple[bool, str]:
    """Gate: is this rural land with a homestead, rather than an urban lot?"""
    folded = _fold(text)
    rural = bool(re.search(TIPO_RURAL, folded))
    urbano = bool(re.search(TIPO_URBANO, folded))
    if rural and not urbano:
        return True, ""
    if rural and urbano:
        return True, "menção a loteamento/condomínio"
    return False, "não parece imóvel rural"


def avaliar(text: str) -> tuple[float, dict, list[str]]:
    """Score a listing.

    Returns (0..1 overall, per-dimension detail, flat evidence labels).
    Dimensions are independent; a listing missing every mention of water scores
    zero there and is heavily penalised through the weighting, but is not
    excluded — rural listings are terse, and silence is not proof of absence.
    """
    folded = _fold(text)
    detalhe: dict[str, dict] = {}
    evidencias: list[str] = []
    total = 0.0

    for nome, spec in DIMENSOES.items():
        sub = 0.0
        provas: list[str] = []

        for pattern, weight, label in spec["positivos"]:
            if _hits(folded, pattern):
                sub += weight
                provas.append(label)

        contagem = spec.get("contagem")
        if contagem:
            pattern, weight, label = contagem
            if re.search(pattern, folded):
                sub += weight
                provas.append(label)

        medida = spec.get("medida")
        if medida:
            delta, label = MEDIDAS[medida](folded)
            if delta is not None:
                sub += delta
                provas.append(label)

        for pattern, weight, label in spec["negativos"]:
            if re.search(pattern, folded):
                sub -= weight
                provas.append("⚠ " + label)

        sub = max(0.0, min(1.0, sub))
        detalhe[nome] = {
            "rotulo": spec["rotulo"],
            "nota": round(sub, 3),
            "peso": spec["peso"],
            "provas": provas,
        }
        evidencias.extend(provas)
        total += sub * spec["peso"]

    return round(total / PESO_TOTAL, 3), detalhe, evidencias
