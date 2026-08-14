"""Weighted scoring of a listing against the fixed buyer profile.

The criteria encoded here are structural — they describe the kind of property
being looked for, not a per-run setting — so they live in code, are versioned,
and are testable. Only size, price and location change per run (criteria.yaml).

Three layers, deliberately kept apart:

1. **Hard discards** (`motivo_descarte`) — reasons to drop a listing outright.
   Structurally different from a penalty: they are not a low score, they are a
   "do not show". `avaliar()` never raises and never returns None; the caller
   decides.
2. **Quality base** — seven weighted dimensions summing to `PESO_TOTAL == 100`.
   Each returns a 0..1 sub-score plus the evidence that produced it, so a card
   can show *why* it ranked where it did rather than an opaque number.
3. **Modifiers** — price per hectare and distance to the centre of interest,
   applied on top of the base and exposed as extra rows in `dimensoes` so the
   page can display them like any other line.

Text matching is accent- and case-insensitive (`_fold`) and negation-aware
(`_hits`): "sem nascente" must never read as water.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- gate

# The property must read as rural land with a homestead. A bare urban lot, a
# house in town, or a plot in a gated development is not what is wanted.
TIPO_RURAL = r"fazenda|chacara|sitio|haras|rancho|area rural|zona rural|propriedade rural|gleba"
TIPO_URBANO = (r"loteamento|condominio fechado|lote urbano|terreno urbano|apartamento"
               r"|sobrado|casa geminada")

# "Chácara"/"Sítio"/"Fazenda" are extremely common as decorative Brazilian
# neighbourhood-name prefixes ("Bairro Chácara Santa Luzia", "Jardim Sítio
# do Sol") for perfectly ordinary urban subdivisions -- the word says
# nothing about the land itself in that role. Found 2026-08-14 against a
# real listing: an urban house ("4 quartos, 2 banheiros, garagem para 2
# carros", zero mention of land size anywhere) in "Chácara Santa Luzia,
# Taubaté" passed the rural gate on that name alone. A bare TIPO_RURAL match
# is no longer trusted by itself -- see `tipo_ok`, which now also requires
# one of these more specific signals before believing it.
_SINAL_RURAL_ESPECIFICO = (
    r"hectares?|\bhas?\b|alqueires?|\bm[²2]\b|\bkm[²2]\b|"
    r"pastage[nm]|pasto|lavoura|\bmata\b|nascente|curral|"
    r"estrada de terra|area rural|zona rural|propriedade rural"
)

# Corroborating evidence that we are looking at rural *land*, not a house on a
# town plot. Used to gate the benfeitorias dimension: buildings only count when
# there is land under them.
CONTEXTO_RURAL = (
    r"fazenda|chacara|sitio|haras|rancho|area rural|zona rural|propriedade rural|gleba"
    r"|hectares?|\bhas?\b|alqueires?|pastage[nm]|pasto|lavoura|mata|nascente|curral"
    r"|estrada de terra|zona de expansao rural|colonia|assentamento"
)

# Below this, a plot is a lot rather than rural land, whatever the ad calls it.
AREA_MIN_RURAL_HA = 0.5

# Beyond this much dirt road the listing is out, not merely penalised.
KM_TERRA_DESCARTE = 8.0

# Sale by unregistered private contract: unfinanceable and unregisterable.
GAVETA = r"contrato de gaveta|contratos? de gaveta"


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _hits(text: str, pattern: str) -> int:
    """Occurrences of `pattern`, ignoring negated *and* exchange mentions.

    "sem nascente" and "não possui água" must not read as positives — the
    single most common way a naive keyword scorer inflates a bad listing. The
    same check runs over the *negative* patterns, so "sem vizinhos próximos"
    does not fire the "vizinhos próximos" penalty.

    "troco por casa" is a second, distinct failure mode found 2026-08-13 (a
    real Facebook ad: "troco com sítio ou casa de meu gosto"): the seller is
    naming what they *want in exchange*, not a feature of *this* property.
    No amount of a longer negation list generalizes past this specific,
    recognizable Brazilian-classifieds phrasing, so it gets its own lookback
    clause rather than being folded into the negation one above (different
    meaning, same mechanism).
    """
    count = 0
    for m in re.finditer(pattern, text):
        before = text[max(0, m.start() - 28):m.start()]
        if re.search(r"\b(sem|nao|nenhum[a]?|falta de|ausencia de|nem)\s+[\w\s]{0,18}$", before):
            continue
        if re.search(r"\b(troco|troca|trocar|aceito troca|em troca de|"
                     r"quero em troca)\s*(?:por|com|de)?\s*[\w\s]{0,18}$", before):
            continue
        count += 1
    return count


# ---------------------------------------------------------------- dimensions
# (pattern, pontos, rótulo). Points inside a dimension are summed, divided by
# the dimension's `escala`, and clipped to 0..1; negatives subtract. Points are
# *relative magnitudes within the dimension*, never absolute score — `escala`
# is what a "full marks" listing looks like for that dimension. Labels are what
# the page shows.

DIMENSOES: dict[str, dict] = {
    "agua": {
        "peso": 30,          # by far the most important criterion
        "rotulo": "Água",
        # nascente 15 · múltiplas nascentes 20 · rio/riacho 15 · lago/açude 10,
        # exactly the owner's table. 40 = one spring plus a watercourse plus a
        # pond, i.e. genuinely water-rich.
        "escala": 40,
        "positivos": [
            (r"nascentes?", 15, "nascente"),
            (r"mina d.?agua|mina de agua|olho d.?agua", 12, "mina d'água"),
            (r"\brios?\b|riachos?|corregos?|ribeiroes?|ribeirao", 15, "rio/riacho"),
            (r"lagoas?|\blagos?\b|represas?|acudes?", 10, "lago/açude"),
            (r"cachoeiras?", 8, "cachoeira"),
            (r"pocos? artesianos?|pocos? semi.?artesianos?", 6, "poço artesiano"),
            (r"rica? em agua|abundancia de agua|muita agua|agua abundante", 12, "água abundante"),
            (r"agua de nascente|agua pura|agua cristalina", 6, "água de qualidade"),
        ],
        "negativos": [
            (r"sem agua|falta de agua|problema de agua|agua escassa", 40, "pouca água"),
        ],
        # More than one nascente is materially better than one: 15 + 5 = 20.
        "contagem": (5, "várias nascentes"),
    },
    "benfeitorias": {
        "peso": 20,
        "rotulo": "Benfeitorias",
        "escala": 100,
        # Rural construction only. Without land under it, a house is a town
        # house and this dimension scores zero — see `requer_rural`.
        "requer_rural": True,
        "positivos": [
            (r"casa sede|sede da fazenda|casa principal", 50, "casa sede"),
            (r"casa (?:do |de )?caseiro|casa de colono", 35, "casa de caseiro"),
            (r"(duas|tres|\b[2-9])\s+casas", 55, "mais de uma casa"),
            (r"\bcasas?\b|residencia|moradia", 35, "casa"),
            (r"currais?|estabulo|mangueira", 20, "curral"),
            (r"galpoes?|galpao|barracao|paiol", 20, "galpão"),
            (r"energia|luz eletrica|rede eletrica|trifasic|monofasic", 18, "energia"),
            (r"cercad[oa]|cercas?\b", 8, "cercado"),
            (r"piscinas?", 5, "piscina"),
        ],
        "negativos": [
            (r"sem benfeitorias|sem construcao|terreno limpo|nenhuma construcao", 100,
             "sem benfeitorias"),
        ],
    },
    "silencio": {
        "peso": 10,
        "rotulo": "Sossego",
        "escala": 100,
        "positivos": [
            (r"sossegad[oa]|sossego|muito silencios[oa]|bem silencios[oa]", 50, "sossegado"),
            (r"sem vizinhos|nenhum vizinho|sem vizinhanca|privacidade total", 50, "sem vizinhos"),
            (r"tranquil[oa]|tranquilidade|paz|paraiso|refugio", 28, "tranquilo"),
            (r"isolad[oa]|reservad[oa]|recolhid[oa]|no fim da estrada|fim de linha",
             30, "reservado"),
            (r"longe da cidade|longe do centro|longe de tudo", 20, "longe da cidade"),
        ],
        "negativos": [
            (r"beira da rodovia|pe na pista|de frente para a rodovia|as margens da br"
             r"|beira de pista|rodovia movimentada|as margens da rodovia",
             65, "na beira da rodovia"),
            (r"barulho|ruido|movimento de caminhoes", 40, "barulho"),
            (r"condominio|loteamento|vizinhos proximos", 45, "vizinhança próxima"),
            (r"proximo ao centro|no centro|area urbana", 35, "perto do centro"),
        ],
    },
    "acessibilidade": {
        # The old `distancia` dimension is folded in here: how far the dirt road
        # runs and how long the drive to town takes are one question.
        "peso": 15,
        "rotulo": "Acesso e distância",
        "escala": 100,
        "positivos": [
            (r"asfalto ate|acesso asfaltad[oa]|totalmente asfaltad[oa]", 50, "asfalto"),
            (r"acesso (?:por |de )?(?:qualquer )?carro|carro de passeio|carro comum",
             45, "acesso por carro comum"),
            (r"bom acesso|otimo acesso|facil acesso|acesso facil|bem acessivel",
             35, "bom acesso"),
            (r"estrada (?:boa|conservada|bem conservada)", 25, "estrada conservada"),
            (r"proxim[oa] a cidade|perto da cidade|proxim[oa] ao comercio|minutos do centro",
             30, "perto da cidade"),
        ],
        "negativos": [
            (r"(?:somente|so|apenas) (?:de |com )?4x4|precisa de 4x4|traca[oa] nas 4",
             100, "só 4x4"),
            (r"dificil acesso|acesso dificil|estrada ruim|estrada precaria",
             70, "acesso difícil"),
        ],
        # "3 km de estrada de terra", "15 min do centro" — parsed, not guessed.
        "medidas": ["estrada_terra", "distancia_cidade"],
    },
    "aptidao": {
        # Merges the old `fertilidade` and `mata` dimensions: what the land is
        # good for agroforestry-wise is one judgement, soil and standing forest
        # together.
        "peso": 10,
        "rotulo": "Aptidão agroflorestal e solo",
        "escala": 100,
        "positivos": [
            (r"mata nativa|floresta nativa|vegetacao nativa|mata virgem", 45, "mata nativa"),
            (r"mata atlantica|cerrado|araucaria|caatinga preservada", 25, "bioma preservado"),
            (r"reserva legal|\brl\b|area de preservacao|\bapp\b", 25, "reserva legal"),
            (r"\bmatas?\b|\bmatinha\b|capoeira|bosque", 20, "mata"),
            (r"nativas? preservad|bem preservad|muito verde", 20, "preservado"),
            (r"terra (?:boa|otima|forte|fertil|roxa|vermelha|preta)|solo fertil"
             r"|terra de cultura|terra preta", 45, "terra boa/fértil"),
            (r"agrofloresta|sistema agroflorestal|\bsaf\b|agroecolog|organic[oa]|permacultura",
             45, "manejo regenerativo"),
            (r"sem agrotoxico|livre de agrotoxico|nunca (?:usou|utilizou) veneno",
             40, "sem agrotóxico"),
            (r"pastage[nm]s?|pasto formad[oa]|braquiaria", 20, "pasto formado"),
            (r"frutiferas?|pomar", 20, "frutíferas"),
        ],
        "negativos": [
            (r"degradad[oa]|erosao|voçoroca|vocoroca|terra fraca|solo pobre|esgotad[oa]",
             80, "degradado"),
            (r"agrotoxic|veneno|pulverizac", 50, "histórico de agrotóxico"),
            (r"monocultura|\bsoja\b|\bcana\b|canavial|algodao", 45, "monocultura"),
            (r"eucalipt|pinus", 25, "eucalipto/pinus"),
            (r"totalmente desmatad|sem mata|sem vegetacao|tudo limpo", 55, "desmatado"),
        ],
    },
    "regularizacao": {
        "peso": 10,
        "rotulo": "Documentação",
        "escala": 100,
        "positivos": [
            (r"escritur|matricula (?:propria|registrada|individual)|registrad[oa] em cartorio"
             r"|titulo definitivo", 60, "escriturado"),
            (r"documentacao em dia|documentos? ok|documentacao regular", 35,
             "documentação em dia"),
            (r"georreferenciad|\bgeo\b|\bccir\b|\bcar\b|\bincra\b|itr em dia", 30,
             "CAR/GEO em dia"),
        ],
        "negativos": [
            # Alert, heavy discount — but the listing still gets shown.
            (r"usucapiao", 75, "usucapião"),
            (GAVETA, 100, "contrato de gaveta"),
            (r"sem escritura|sem matricula|apenas posse|so posse|documentacao irregular",
             80, "sem escritura"),
            (r"inventario|espolio|litigio|penhora|acao judicial", 50, "pendência judicial"),
        ],
    },
    "topografia": {
        "peso": 5,
        "rotulo": "Topografia",
        "escala": 100,
        "positivos": [
            (r"\bplan[oa]s?\b|terreno plano|area plana|totalmente plan[oa]", 50, "plano"),
            (r"levemente ondulad[oa]|pouca declividade|suave ondulad[oa]|baixa declividade"
             r"|aclive suave|declive suave", 40, "aclive suave"),
            (r"partes? baixas? aproveitav|baixada aproveitav|varzea aproveitav", 30,
             "partes baixas aproveitáveis"),
            (r"boa topografia|topografia excelente|topografia favoravel", 30, "boa topografia"),
        ],
        "negativos": [
            (r"muito acidentad[oa]|declive acentuad[oa]|ingreme|montanhos[oa]|so serra",
             65, "acidentado"),
            (r"\bacidentad[oa]\b", 40, "acidentado"),
        ],
        "medidas": ["percentual_plano"],
    },
}

PESO_TOTAL = sum(d["peso"] for d in DIMENSOES.values())  # 100 by construction

# ---------------------------------------------------------------- modifiers
# Defaults, overridable per call so criteria.yaml can drive them.
PRECO_HA_BOM = 100_000.0        # below this: meaningful bonus
PRECO_HA_LIMITE = 150_000.0     # above this: clear penalty (never a discard)
PRECO_HA_BONUS_MAX = 0.10
PRECO_HA_PENALIDADE_MAX = 0.15

CENTRO_PERTO_KM = 15.0          # strong bonus
CENTRO_MEDIO_KM = 40.0          # moderate bonus
CENTRO_NEUTRO_KM = 70.0         # neutral; beyond this a growing penalty
CENTRO_BONUS_MAX = 0.10
CENTRO_PENALIDADE_MAX = 0.15

# Named-zone tiers (2026-08-13), on top of the continuous distance curve
# below rather than replacing it -- see `ajuste_zona`. Levels 2 and 3 are
# fixed bonuses for being in a specific, named municipality (hand-drawn
# zones on a map, translated to town lists -- see criteria.yaml); level 4 is
# reserved for `centro` itself and is deliberately the highest of all four,
# per the owner's explicit "4th overall best when in Monteiro Lobato
# exactly." Level 1 has no fixed bonus of its own: an unnamed municipality
# (or an unknown one) falls through to `ajuste_centro`'s distance curve,
# which is what always governed proximity before this tiering existed.
ZONA_BOA_BONUS = 0.06
ZONA_MELHOR_BONUS = 0.10
ZONA_CENTRO_BONUS = 0.13


# ---------------------------------------------------------------- measures
def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def km_estrada_terra(text: str) -> float | None:
    """Kilometres of dirt road stated in the listing, or None.

    `text` may be raw or folded — it is folded here either way.
    """
    folded = _fold(text)
    m = re.search(r"([\d.,]+)\s*(?:km|quilometros?)\s+(?:de\s+)?(?:estrada\s+de\s+)?"
                  r"(?:terra|chao|nao pavimentad)", folded)
    if not m:
        m = re.search(r"(?:estrada\s+de\s+)?(?:terra|chao)[^.]{0,20}?([\d.,]+)\s*km", folded)
    if not m:
        return None
    return _num(m.group(1))


def _dist_terra(text: str) -> tuple[float | None, str]:
    """Dirt road, in dimension points. ≤2 km is the target, >5 km hurts.

    Beyond `KM_TERRA_DESCARTE` the listing is discarded upstream by
    `motivo_descarte`; the steep penalty here is only the fallback for a caller
    that chooses not to discard.
    """
    km = km_estrada_terra(text)
    if km is None:
        return None, ""
    if km <= 2:
        return 45, f"{km:g} km de terra"
    if km <= 5:
        return 15, f"{km:g} km de terra"
    if km <= KM_TERRA_DESCARTE:
        return -60, f"{km:g} km de terra"
    return -100, f"{km:g} km de terra"


def _dist_cidade(text: str) -> tuple[float | None, str]:
    """Minutes or kilometres to town. The target is 18 minutes or less."""
    m = re.search(r"([\d.,]+)\s*(?:min|minutos?)\s+(?:d[aeo]s?\s+)?"
                  r"(?:cidade|centro|comercio|supermercado|asfalto)", text)
    if m:
        minutos = _num(m.group(1))
        if minutos is None:
            return None, ""
        if minutos <= 18:
            return 70, f"{minutos:g} min da cidade"
        if minutos <= 30:
            return 10, f"{minutos:g} min da cidade"
        return -50, f"{minutos:g} min da cidade"

    m = re.search(r"([\d.,]+)\s*(?:km|quilometros?)\s+(?:d[aeo]s?\s+)?(?:cidade|centro)", text)
    if m:
        km = _num(m.group(1))
        if km is None:
            return None, ""
        # Roughly 40 km/h on the mixed roads involved: 18 min ≈ 12 km.
        if km <= 12:
            return 70, f"{km:g} km da cidade"
        if km <= 25:
            return 10, f"{km:g} km da cidade"
        return -50, f"{km:g} km da cidade"
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
        return 55, f"{pct}% plano"
    if pct >= 30:
        return 20, f"{pct}% plano"
    return -20, f"{pct}% plano"


MEDIDAS = {
    "estrada_terra": _dist_terra,
    "distancia_cidade": _dist_cidade,
    "percentual_plano": _percentual_plano,
}

_PALAVRA_NUM = {"duas": 2, "dois": 2, "tres": 3, "quatro": 4, "cinco": 5,
                "seis": 6, "sete": 7, "oito": 8, "nove": 9}


def n_nascentes(text: str) -> int | None:
    """How many springs the listing claims, when it states a count.

    "várias nascentes" returns None — plural is known, the number is not, and
    inventing "2" would put a number on the card the ad never gave.
    """
    folded = _fold(text)
    m = re.search(r"(duas|dois|tres|quatro|cinco|seis|sete|oito|nove|varias|varios"
                  r"|multiplas|diversas|\d{1,2})\s+nascentes", folded)
    if not m:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    return _PALAVRA_NUM.get(raw)


# ---------------------------------------------------------------- gate/discard
def tipo_ok(text: str) -> tuple[bool, str]:
    """Gate: is this rural land with a homestead, rather than an urban lot?"""
    folded = _fold(text)
    rural = bool(re.search(TIPO_RURAL, folded))
    urbano = bool(re.search(TIPO_URBANO, folded))
    if rural and not urbano:
        # A bare "chácara"/"sítio"/"fazenda" is trusted only alongside a more
        # specific rural signal -- see _SINAL_RURAL_ESPECIFICO's docstring.
        if not re.search(_SINAL_RURAL_ESPECIFICO, folded):
            return False, "menção a chácara/sítio/fazenda sem outro sinal rural (pode ser nome de bairro)"
        return True, ""
    if rural and urbano:
        return True, "menção a loteamento/condomínio"
    return False, "não parece imóvel rural"


def motivo_descarte(text: str, area_ha: float | None = None) -> str | None:
    """Short Portuguese reason to drop the listing outright, else None.

    A discard is structurally different from a penalty: the listing does not
    belong on the website at all. Conditions:

    * fails the `tipo_ok` rural gate (urban lot, town house, flat);
    * more than `KM_TERRA_DESCARTE` km of dirt road;
    * `contrato de gaveta` — unregisterable title;
    * `area_ha` below `AREA_MIN_RURAL_HA`, when known: a lot, not rural land.

    A too-high price per hectare is *not* here on purpose — it is a penalty.
    """
    folded = _fold(text)

    ok, _ = tipo_ok(text)
    if not ok:
        return "não parece imóvel rural"

    km = km_estrada_terra(folded)
    if km is not None and km > KM_TERRA_DESCARTE:
        return f"{km:g} km de estrada de terra (limite {KM_TERRA_DESCARTE:g} km)"

    if _hits(folded, GAVETA):
        return "contrato de gaveta"

    if area_ha is not None and area_ha < AREA_MIN_RURAL_HA:
        return f"área de {area_ha:g} ha — lote, não imóvel rural"

    return None


# ---------------------------------------------------------------- modifiers
def ajuste_preco_ha(
    price_per_ha: float | None,
    bom: float = PRECO_HA_BOM,
    limite: float = PRECO_HA_LIMITE,
) -> tuple[float, float, str]:
    """Price-per-hectare modifier: (nota 0..1 for display, score delta, label).

    Below `bom` a meaningful bonus, between `bom` and `limite` neutral to
    slightly negative, above `limite` a clear penalty — never a discard.
    Unknown price is neutral.
    """
    if not price_per_ha or price_per_ha <= 0:
        return 0.5, 0.0, ""

    rotulo = f"R$/ha {price_per_ha:,.0f}".replace(",", ".")
    if price_per_ha < bom:
        # Full bonus at half the "good" threshold, tapering to zero at it.
        fracao = min(1.0, (bom - price_per_ha) / (bom * 0.5))
        return 0.5 + 0.5 * fracao, round(PRECO_HA_BONUS_MAX * fracao, 4), rotulo
    if price_per_ha <= limite:
        fracao = (price_per_ha - bom) / max(1.0, limite - bom)
        return 0.5 - 0.2 * fracao, round(-0.02 * fracao, 4), rotulo
    # Full penalty by the time it is half again over the limit.
    fracao = min(1.0, (price_per_ha - limite) / (limite * 0.5))
    return max(0.0, 0.3 - 0.3 * fracao), round(-PRECO_HA_PENALIDADE_MAX * fracao, 4), \
        "⚠ " + rotulo


def ajuste_centro(distancia_centro_km: float | None) -> tuple[float, float, str]:
    """Proximity-to-centre modifier: (nota 0..1, score delta, label).

    Monteiro Lobato, SP is the primary target area; closer is strictly better.
    The distance is supplied by the caller (pipeline computes it with
    `geo.haversine_km` against `criteria.centro`) — nothing is geocoded here.
    `None` is neutral: an unknown location is silence, not evidence.
    """
    km = distancia_centro_km
    if km is None:
        return 0.5, 0.0, ""

    km = max(0.0, float(km))
    rotulo = f"{km:g} km do centro de interesse"
    if km <= CENTRO_PERTO_KM:
        fracao = 1.0 - km / CENTRO_PERTO_KM * 0.3      # 1.0 at 0 km, 0.7 at 15 km
        return 0.85 + 0.15 * fracao, round(CENTRO_BONUS_MAX * fracao, 4), rotulo
    if km <= CENTRO_MEDIO_KM:
        fracao = (CENTRO_MEDIO_KM - km) / (CENTRO_MEDIO_KM - CENTRO_PERTO_KM)
        return 0.6 + 0.25 * fracao, round(CENTRO_BONUS_MAX * 0.5 * fracao, 4), rotulo
    if km <= CENTRO_NEUTRO_KM:
        fracao = (CENTRO_NEUTRO_KM - km) / (CENTRO_NEUTRO_KM - CENTRO_MEDIO_KM)
        return 0.5 + 0.1 * fracao, 0.0, rotulo
    # Growing penalty, full by 70 km beyond the neutral band.
    fracao = min(1.0, (km - CENTRO_NEUTRO_KM) / CENTRO_NEUTRO_KM)
    return max(0.0, 0.5 - 0.5 * fracao), round(-CENTRO_PENALIDADE_MAX * fracao, 4), \
        "⚠ " + rotulo


def ajuste_zona(
    municipality: str,
    centro: str | None,
    zona_melhor: list[str],
    zona_boa: list[str],
    distancia_centro_km: float | None,
) -> tuple[float, float, str]:
    """4-tier proximity modifier: (nota 0..1, score delta, label).

    Tiers 2-4 are a *named-municipality* match, not a distance measurement --
    "estar em Monteiro Lobato" is a category the owner named directly (two
    hand-drawn map zones translated to town lists, see criteria.yaml), not a
    radius. Tier 1 has no list of its own: any municipality not named in
    `centro`/`zona_melhor`/`zona_boa` — including an unknown one — falls
    through to `ajuste_centro`'s continuous distance curve, exactly as
    proximity worked before this tiering existed.

    An empty `municipality` always falls through to tier 1: a listing whose
    town could not be read gets no credit for a tier that requires naming it.
    """
    muni = _fold(municipality)
    if muni:
        if centro and muni == _fold(centro.split(",")[0]):
            return 1.0, ZONA_CENTRO_BONUS, f"em {municipality.strip()} (centro exato)"
        if muni in {_fold(m) for m in zona_melhor}:
            return 0.92, ZONA_MELHOR_BONUS, f"em {municipality.strip()} (zona melhor)"
        if muni in {_fold(m) for m in zona_boa}:
            return 0.75, ZONA_BOA_BONUS, f"em {municipality.strip()} (zona boa)"
    return ajuste_centro(distancia_centro_km)


# ---------------------------------------------------------------- scoring
# A specific label makes its generic cousin redundant *on the card* — "casa
# sede + casa de caseiro + casa" reads badly. Scoring is untouched; only the
# evidence list is pruned.
_REDUNDANTES: dict[str, tuple[str, ...]] = {
    "casa sede": ("casa",),
    "casa de caseiro": ("casa",),
    "mais de uma casa": ("casa",),
    "mata nativa": ("mata", "preservado"),
    "várias nascentes": ("nascente",),
    "asfalto": ("bom acesso",),
}


def _podar(provas: list[str]) -> list[str]:
    redundantes: set[str] = set()
    for prova in provas:
        redundantes.update(_REDUNDANTES.get(prova, ()))
        if re.fullmatch(r"\d+ nascentes", prova):
            redundantes.add("nascente")
    return [p for p in provas if p not in redundantes]


def _sub_score(spec: dict, folded: str) -> tuple[float, list[str]]:
    """One dimension: its 0..1 sub-score and the evidence labels behind it."""
    pontos = 0.0
    provas: list[str] = []

    if spec.get("requer_rural") and not re.search(CONTEXTO_RURAL, folded):
        # An isolated house with no rural land earns nothing here.
        return 0.0, ["⚠ sem contexto rural"]

    for pattern, pts, label in spec["positivos"]:
        if _hits(folded, pattern):
            pontos += pts
            provas.append(label)

    contagem = spec.get("contagem")
    if contagem:
        pts, label = contagem
        n = n_nascentes(folded)
        if n and n >= 2:
            pontos += pts
            provas.append(f"{n} nascentes")
        elif _hits(folded, r"nascentes\b"):
            # Bare plural: more than one, count unstated.
            pontos += pts
            provas.append(label)

    for medida in spec.get("medidas", []):
        delta, label = MEDIDAS[medida](folded)
        if delta is not None:
            pontos += delta
            provas.append(label)

    for pattern, pts, label in spec["negativos"]:
        if _hits(folded, pattern):
            pontos -= pts
            provas.append("⚠ " + label)

    sub = max(0.0, min(1.0, pontos / float(spec.get("escala", 100))))
    return sub, _podar(provas)


ESTRELAS: list[tuple[str, str]] = [
    # Order matters: cachoeira first, it is the headline standout.
    (r"cachoeiras?", "Cachoeira"),
    (r"(?:duas|dois|tres|quatro|cinco|varias|varios|multiplas|diversas|\d{1,2})\s+nascentes"
     r"|nascentes\b", "Múltiplas nascentes"),
    (r"agrofloresta|sistema agroflorestal|\bsaf\b|agroecolog|permacultura",
     "Agrofloresta/SAF já implantada"),
    (r"sem agrotoxico|livre de agrotoxico|nunca (?:usou|utilizou) veneno", "Sem agrotóxico"),
    (r"mata nativa|floresta nativa|mata virgem", "Mata nativa preservada"),
    (r"terra (?:preta|roxa)|solo muito fertil", "Terra preta/roxa"),
]


# Facebook Marketplace (and other aggregators without a custom title) auto-
# generate a title from the listing's own category tag alone -- "Estúdio 0
# banheiros – Casa", "2 quartos 1 banheiro – Casa" -- where "Casa" is the
# *property-type category*, not a claim the land has a house on it. Found
# 2026-08-13 against a real listing (a bare rural lot, explicitly "ideal
# para CONSTRUIR a chácara dos seus sonhos") whose auto-title's "Casa"
# nonetheless scored a real "benfeitorias" hit. Titles matching this shape
# carry no genuine feature information, so the caller should score the
# description alone, not title + description, for exactly these.
TITULO_GENERICO_RE = re.compile(
    r"^\s*(?:est[uú]dio|\d+\s*quartos?)\s+\d+\s*banheiros?\s*[-–]?\s*"
    r"(?:casa|apartamento|studio|kitnet|sobrado|cobertura)\s*$",
    re.I,
)


def titulo_generico(title: str) -> bool:
    """Whether `title` is an auto-generated category label, not real prose."""
    return bool(TITULO_GENERICO_RE.match((title or "").strip()))


def estrelas(text: str) -> list[str]:
    """Standout features worth flagging, not scored — surfaced.

    `cachoeira` is explicitly *not* mandatory and is not a criterion, but the
    owner wants to see it whenever it is there, so it leads the list. (It still
    contributes a small amount to the água dimension, because a waterfall is
    real water; the star is the *surfacing*, not the score.)
    """
    folded = _fold(text)
    fora: list[str] = []
    for pattern, label in ESTRELAS:
        if _hits(folded, pattern) and label not in fora:
            fora.append(label)
    return fora


# Which dimensions feed which notification theme. Keys are the exact names the
# Telegram card in notify.py expects.
_TEMAS: dict[str, tuple[str, ...]] = {
    "agua": ("agua",),
    "benfeitorias": ("benfeitorias",),
    "acesso": ("acessibilidade",),
    "documentacao": ("regularizacao",),
    "solo": ("aptidao", "topografia"),
}


def destaques(detalhe: dict) -> dict[str, str]:
    """Short Portuguese one-liners per theme, for the notification card.

    Built only from evidence already collected by `avaliar()` — pass it the
    `dimensoes` dict. A theme with no evidence is simply absent; nothing is
    invented or padded. Warnings keep their "⚠" so a usucapião listing does not
    read as clean documentation.

    Keys, when present: agua, benfeitorias, acesso, documentacao, solo.

        {"agua": "2 nascentes + rio/riacho",
         "acesso": "1,5 km de terra + 12 min da cidade"}
    """
    fora: dict[str, str] = {}
    for tema, dims in _TEMAS.items():
        provas: list[str] = []
        for nome in dims:
            for prova in (detalhe.get(nome) or {}).get("provas") or []:
                if prova and prova not in provas:
                    provas.append(prova)
        if not provas:
            continue
        linha = " + ".join(provas[:4])
        fora[tema] = linha[0].upper() + linha[1:] if linha[0].isalpha() else linha
    return fora


def avaliar(
    text: str,
    price_per_ha: float | None = None,
    distancia_centro_km: float | None = None,
    preco_ha_bom: float = PRECO_HA_BOM,
    preco_ha_limite: float = PRECO_HA_LIMITE,
    municipality: str = "",
    centro: str | None = None,
    zona_melhor: list[str] | None = None,
    zona_boa: list[str] | None = None,
) -> tuple[float, dict, list[str], list[str]]:
    """Score a listing. Never raises, never returns None — discards are separate.

    Contract
    --------
    Arguments (all but `text` optional keyword arguments with defaults, so the
    old one-argument call still works):

    * `text` — title plus description, raw (folded internally).
    * `price_per_ha` — R$/ha, or None when unknown (neutral, not penalised).
    * `distancia_centro_km` — km from `criteria.centro`, computed by the caller
      with `geo.haversine_km`. None is neutral. Nothing is geocoded here.
    * `preco_ha_bom` / `preco_ha_limite` — R$/ha thresholds, defaulting to
      `PRECO_HA_BOM` (100 000) and `PRECO_HA_LIMITE` (150 000); wire these to
      criteria.yaml.
    * `municipality`/`centro`/`zona_melhor`/`zona_boa` — the 4-tier proximity
      bonus (see `ajuste_zona`); when `municipality` is empty or matches none
      of the three, falls through to the plain distance curve exactly as
      before this tiering existed.

    Returns a **4-tuple**. The first three keep exactly their previous meaning;
    the fourth is new:

    0. `nota: float` — overall 0..1, rounded to 3 places, clamped. It is the
       weighted mean of the seven dimensions (weights summing to `PESO_TOTAL`
       == 100) plus the two modifiers' deltas.
    1. `dimensoes: dict[str, dict]` — one entry per dimension, each
       `{"rotulo", "nota" (0..1), "peso" (int), "provas": list[str]}`.
       Two extra rows are appended for the modifiers, `"preco_ha"` and
       `"proximidade_centro"`; they carry the same four keys — with `"peso": 0`,
       since they are not part of the 100-point base — plus `"ajuste"`, the
       signed delta they applied to `nota`. A display layer that just walks the
       dict and draws `nota` needs no change.
    2. `evidencias: list[str]` — flat list of every evidence label, warnings
       prefixed "⚠", in dimension order, modifiers last.
    3. `estrelas: list[str]` — standout features to highlight (cachoeira first).
       Not scored. Same as calling `estrelas(text)`.

    For the notification card, pass the returned `dimensoes` to `destaques()`.
    """
    folded = _fold(text)
    detalhe: dict[str, dict] = {}
    evidencias: list[str] = []
    total = 0.0

    for nome, spec in DIMENSOES.items():
        sub, provas = _sub_score(spec, folded)
        detalhe[nome] = {
            "rotulo": spec["rotulo"],
            "nota": round(sub, 3),
            "peso": spec["peso"],
            "provas": provas,
        }
        evidencias.extend(provas)
        total += sub * spec["peso"]

    nota = total / PESO_TOTAL

    nota_preco, ajuste_p, prova_p = ajuste_preco_ha(price_per_ha, preco_ha_bom, preco_ha_limite)
    detalhe["preco_ha"] = {
        "rotulo": "Preço por hectare",
        "nota": round(nota_preco, 3),
        "peso": 0,
        "provas": [prova_p] if prova_p else [],
        "ajuste": ajuste_p,
    }

    nota_centro, ajuste_c, prova_c = ajuste_zona(
        municipality, centro, zona_melhor or [], zona_boa or [], distancia_centro_km,
    )
    detalhe["proximidade_centro"] = {
        "rotulo": "Proximidade do centro",
        "nota": round(nota_centro, 3),
        "peso": 0,
        "provas": [prova_c] if prova_c else [],
        "ajuste": ajuste_c,
    }

    for prova in (prova_p, prova_c):
        if prova:
            evidencias.append(prova)

    nota = max(0.0, min(1.0, nota + ajuste_p + ajuste_c))
    return round(nota, 3), detalhe, evidencias, estrelas(text)
