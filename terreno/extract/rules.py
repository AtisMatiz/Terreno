"""Rules-based extraction of a listing from an arbitrary page.

This is the default extractor for Layer B and costs nothing. It tries, in
order: JSON-LD (best), OpenGraph/meta tags, then the visible text. The LLM
extractor is only consulted when this returns something too thin to use.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urlsplit

from ..datas import encontrar_data_pt, parse_data
from ..models import Listing
from ..sources.base import json_ld, strip_tags
from ..units import area_to_ha, parse_number, price_to_brl

log = logging.getLogger("terreno.extract.rules")

_META = r'<meta[^>]+(?:property|name)=["\']{key}["\'][^>]+content=["\']([^"\']*)["\']'

# Municipality/UF as it is normally written in a Brazilian listing.
_LOCATION_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç'’\-]+"
    r"(?:\s+(?:de|do|da|dos|das)\s+[\w\-áéíóúâêôãõç]+|\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç\-]+)*)"
    r"\s*[-/,]\s*(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b"
)

# Fallback for the punctuation-less, lowercase/unaccented shape Facebook's own
# `listingTitle` field sometimes returns (confirmed 2026-08-25 against a real
# listing: "chacara ... em condominio fechado em ribeirao preto sp" -- no
# accents, no comma/slash before the state). `_LOCATION_RE` above requires a
# capitalized city and an explicit "-", "/" or "," before the UF, so it never
# matched this shape at all -- silently leaving `municipality`/`uf` blank and,
# through that, disabling the radius filter in `pipeline.apply_filters` for
# exactly the out-of-region listings that filter exists to catch (a real
# Ribeirão Preto/SP listing -- 280+ km outside the configured region -- still
# reached Telegram because of this gap). Anchored to end-of-string (optionally
# before a trailing "." or "!"): a bare state abbreviation loose in the middle
# of prose is not trustworthy, but one immediately before the ad text ends is
# the ordinary "...cidade, UF" shape with just the punctuation missing.
# `_clean_municipality` below still trims the captured group down to the
# municipality itself.
_LOCATION_RE_SOLTO = re.compile(
    r"\b((?:[a-záéíóúâêôãõç]+\s+){1,4}?[a-záéíóúâêôãõç]+)\s+"
    r"(ac|al|ap|am|ba|ce|df|es|go|ma|mt|ms|mg|pa|pb|pr|pe|pi|rj|rn|rs|ro|rr|sc|sp|se|to)"
    r"\s*[.!]?\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------------- páginas genéricas
#
# What reaches this extractor from Layer B is "whatever Brave returned", which
# includes a lot of pages that are *about* rural property without *being* one
# offer: agency home pages, broker profiles, blog listicles, search results.
# Every one of those that slips through becomes a Telegram message with a link
# that answers none of the owner's questions.
#
# The asymmetry is deliberate and the whole design rests on it: dropping one
# genuine listing costs almost nothing (the goal is a short, high-quality feed,
# and the same plot is usually cross-posted on several portals anyway), while a
# single junk link is the failure actually being complained about. So each
# guard below is allowed to be somewhat trigger-happy, and the reason it fired
# is logged at debug so an over-blocking guard can be found and loosened
# without guessing.

# Path segments that belong to a site's *navigation*, never to one offer.
# Terminated by a hyphen as well as by "/" so "/corretor-de-imoveis-sp" and
# "/imobiliaria-em-sao-jose-dos-campos" match too.
_URL_NAO_ANUNCIO = re.compile(
    r"/(?:imobiliaria|imobiliarias|corretor|corretores|corretora|consultoria|"
    r"blog|noticia|noticias|artigo|artigos|dicas|guia|"
    r"busca|buscar|buscas|search|pesquisa|resultado|resultados|"
    r"lista|listas|listagem|categoria|categorias|tag|tags|"
    r"quem-somos|sobre|sobre-nos|contato|equipe|time|anuncie|anunciar|"
    r"depoimentos|servicos|servico)(?:[-/?#]|$)",
    re.I,
)

# Something in the URL that identifies *this* property: a portal id, or a path
# segment portals use for a single listing's page.
_URL_ID_NUMERICO = re.compile(r"(?:^|[/=_-])(\d{5,})(?=$|[/._&?-])")
_URL_SEGMENTO_ANUNCIO = re.compile(
    r"/(?:imovel|anuncio|propriedade|ficha|detalhe|detalhes|item|oferta|listing)(?:[-/]|$)",
    re.I,
)
_URL_ID_PARAM = re.compile(r"[?&](?:id|codigo|cod|ref|imovel|imovel_id)=[\w-]+", re.I)

# "2 Melhores Sítios à Venda em ...", "Os 10 melhores sítios ..."
_TITULO_LISTICLE = re.compile(
    r"^\W*(?:os|as)?\s*\d{1,3}\s+(?:melhor|melhores|maiores|mais|op[çc][õo]es|"
    r"im[óo]ve|s[íi]tio|ch[áa]cara|fazenda|terreno|casa|lote|[áa]rea)",
    re.I,
)
# A plural property noun offered for sale is an inventory page, not an offer.
_TITULO_PLURAL = re.compile(
    r"\b(?:s[íi]tios|ch[áa]caras|fazendas|terrenos|im[óo]veis|casas|lotes|"
    r"[áa]reas|propriedades)\b[^.|]{0,40}?\b(?:[àa]\s+venda|para\s+vender|"
    r"dispon[íi]veis|em\s+\d{4})",
    re.I,
)
# "Chácaras e sítios em X" — plural inventory without an explicit "à venda".
_TITULO_PLURAL_DUPLO = re.compile(
    r"\b(?:s[íi]tios|ch[áa]caras|fazendas|terrenos|lotes)\b\s*(?:,|e|/|\+)\s*"
    r"\b(?:s[íi]tios|ch[áa]caras|fazendas|terrenos|lotes)\b",
    re.I,
)
# The page describing whoever is selling, rather than what is for sale.
_TITULO_AGENCIA = re.compile(
    r"consultoria\s+imobili[áa]ria|assessoria\s+imobili[áa]ria|"
    r"especialista\s+em|corretor(?:a)?\s+de\s+im[óo]veis|imobili[áa]ria\s+em|"
    r"quem\s+somos|sobre\s+n[óo]s|anuncie\s+seu\s+im[óo]vel|"
    r"encontre\s+(?:seu|o|os)\s+(?:melhor(?:es)?\s+)?im[óo]ve|"
    r"os\s+melhores\s+im[óo]veis|compra\s+e\s+venda\s+de\s+im[óo]veis",
    re.I,
)

# Result-set furniture. These only exist where several properties are listed.
_CORPO_INDICE = re.compile(
    r"\b\d{1,6}\s+im[óo]ve(?:l|is)\s+encontrad|"
    r"\b\d{1,6}\s+an[úu]ncios?\s+encontrad|"
    r"\b\d{1,6}\s+resultados?\s+encontrad|"
    r"p[áa]gina\s+\d+\s+de\s+\d+|"
    r"resultados?\s+da\s+busca|refine\s+sua\s+busca|"
    r"\b\d{1,6}\s+im[óo]veis\s+(?:[àa]\s+venda|dispon[íi]veis)\b",
    re.I,
)
# Weaker: a real listing page occasionally carries a "similar properties"
# widget with these. Only blocks when the URL also has no per-listing id.
_CORPO_BUSCA_FRACO = re.compile(
    r"ordenar\s+por|filtrar\s+por|pr[óo]xima\s+p[áa]gina|ver\s+mais\s+im[óo]veis",
    re.I,
)

_AREA_MENCAO = re.compile(
    r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(alqueires?|hectares?|ha|m²|m2|km²|km2)\b",
    re.I,
)
_AREA_FATOR = {
    "alqueire": 2.42, "alqueires": 2.42, "hectare": 1.0, "hectares": 1.0,
    "ha": 1.0, "m²": 1e-4, "m2": 1e-4, "km²": 100.0, "km2": 100.0,
}


def _tem_id_de_anuncio(url: str) -> bool:
    """Whether the URL points at one identified property rather than a section."""
    partes = urlsplit(url)
    caminho = partes.path
    return bool(
        _URL_ID_NUMERICO.search(caminho)
        or _URL_SEGMENTO_ANUNCIO.search(caminho)
        or _URL_ID_PARAM.search("?" + partes.query)
    )


def _areas_distintas(texto: str) -> int:
    """How many distinct *land-scale* areas the page mentions.

    Built areas ("casa de 180 m²") are excluded by the 0.5 ha floor, so this
    counts plots, and three plots on one page means an index, exactly as three
    distinct prices does.
    """
    vistos: set[float] = set()
    for numero, unidade in _AREA_MENCAO.findall(texto):
        valor = parse_number(numero)
        if valor is None:
            continue
        ha = valor * _AREA_FATOR.get(unidade.lower(), 0.0)
        if ha >= 0.5:
            vistos.add(round(ha, 2))
    return len(vistos)


def _motivo_generica(url: str, title: str, body: str, haystack: str,
                     price: float | None, area_ha: float | None,
                     estruturado: bool = False) -> str:
    """Name of the guard that says this page is not a single offer, or ""."""
    tem_id = _tem_id_de_anuncio(url)
    titulo_e_corpo = f"{title}\n{body}"

    if _URL_NAO_ANUNCIO.search(urlsplit(url).path):
        return "url_de_secao"
    if _TITULO_LISTICLE.search(title):
        return "titulo_listicle"
    if _TITULO_PLURAL.search(title):
        return "titulo_plural"
    if _TITULO_PLURAL_DUPLO.search(title):
        return "titulo_plural_duplo"
    if _TITULO_AGENCIA.search(title):
        return "titulo_de_agencia"
    if _CORPO_INDICE.search(titulo_e_corpo):
        return "marcadores_de_resultado"
    # A área NUNCA vem de dado estruturado neste módulo -- é sempre extraída
    # de texto livre (ver extract()), então o risco de contaminação por uma
    # vitrine de "imóveis semelhantes" na mesma página vale tenha ou não um
    # preço estruturado ao lado. Corrigido 2026-08-14: este guard ficava
    # atrás do `if estruturado: return ""` abaixo, e por isso nunca disparava
    # nessas páginas -- exatamente o caso real que expôs o bug (uma casa
    # urbana comum, "Chácara Santa Luzia" sendo o nome do bairro, relatada
    # com 4,7 ha que na verdade pertenciam a outro imóvel na mesma página).
    if _areas_distintas(haystack) >= 3:
        return "muitas_areas"
    # A partir daqui os sinais restantes são sobre preço/id, onde a mesma
    # isenção realmente vale: uma página cuja estrutura declara *um* anúncio
    # com preço está falando de um imóvel específico.
    if estruturado:
        return ""
    if not tem_id:
        # No id in the URL is not damning by itself (plenty of agency sites use
        # a bare slug), but combined with either of these it is.
        if price is not None and area_ha is None:
            # "área n/d" plus an unidentifiable URL: two of the four links the
            # owner complained about looked exactly like this.
            return "preco_sem_area_sem_id"
        if _CORPO_BUSCA_FRACO.search(body):
            return "corpo_de_busca_sem_id"
    return ""


def meta(html: str, key: str) -> str:
    m = re.search(_META.format(key=re.escape(key)), html, re.I)
    return m.group(1).strip() if m else ""


def municipio_do_texto(texto: str) -> tuple[str, str]:
    """Best-effort (municipality, uf) parsed from free Portuguese prose, or
    ("", "") when nothing can be trimmed confidently.

    Public and shared (not just this extractor's own use below): a source
    with its own *structured* location data (facebook.py, from the Apify
    actor's `location` field) still needs this whenever that field is empty
    or missing -- found 2026-08-17 against two real listings (Joanópolis/SP,
    Rio Claro/SP) where Facebook's own geo data was blank but the seller's
    own description named the city in plain text, in a shape this parser
    already reads correctly. Without this fallback, both listings kept
    `municipality=""`, which silently disables the region whitelist and
    radius filters in `pipeline.apply_filters` -- not a wrong answer, just no
    answer, but the practical effect was two listings 100+ km outside the
    configured search region reaching Telegram anyway."""
    m = _LOCATION_RE.search(texto)
    if not m:
        m = _LOCATION_RE_SOLTO.search(texto.strip())
    if not m:
        return "", ""
    return _clean_municipality(m.group(1)), m.group(2).upper()


def _data_publicada(html: str, haystack: str) -> str:
    """Whatever publication date this page states, raw (not yet parsed) --
    `Listing.posted_at` for a Brave-found page, feeding the too-old filter in
    `pipeline.apply_filters`. JSON-LD first (most reliable, when present),
    then OpenGraph, then a bare "26 de fevereiro de 2018" in the visible
    text -- exactly what a LinkedIn "pulse" post's byline looks like, the
    kind of page that has neither of the other two but is still, visibly,
    eight years old. "" when nothing recognizable is found; that stays
    "unknown", never "old", same as everywhere else this field is used."""
    for node in json_ld(html):
        for campo in ("datePublished", "dateModified"):
            val = node.get(campo)
            if val and parse_data(str(val)):
                return str(val)
    for campo in ("article:published_time", "article:modified_time", "og:updated_time"):
        val = meta(html, campo)
        if val and parse_data(val):
            return val
    return encontrar_data_pt(haystack)


def extract(html: str, url: str, source: str = "brave") -> Listing | None:
    """Best-effort structured listing from a page. None when the page clearly
    is not an individual offer."""
    # Cheapest guard first: a URL that is a section of a site rather than one
    # property needs no parsing at all to be rejected.
    if _URL_NAO_ANUNCIO.search(urlsplit(url).path):
        log.debug("descartado (url_de_secao): %s", url)
        return None

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

    # The location is resolved *before* the area, not after: the UF is what
    # decides which alqueire a "3 alqueires" listing means, and without it the
    # area is now (correctly) left unknown rather than guessed. Reading the
    # municipality first turns most of those unknowns back into real hectares.
    if not municipality:
        municipality, uf_achado = municipio_do_texto(f"{title} {description}")
        if not municipality:
            municipality, uf_achado = municipio_do_texto(body)
        uf = uf_achado or uf

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
        log.debug("descartado (muitos_precos): %s", url)
        return None

    motivo = _motivo_generica(url, title, body, haystack, price, area_ha,
                              estruturado=preco_estruturado)
    if motivo:
        log.debug("descartado (%s): %s", motivo, url)
        return None

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
        posted_at=_data_publicada(html, haystack),
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
