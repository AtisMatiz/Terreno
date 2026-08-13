"""Layer C — Facebook Marketplace and groups.

Two interchangeable backends, in this order:

  1. Apify actor, while the free monthly credit lasts. Guarded twice: against
     Apify's own reported limits and against our local ledger.
  2. Local Playwright with a burner account's cookies, for anything the actor
     cannot reach (member-only groups) and for when the credit runs out.

Backend 2 is intended to run on your own machine — `scripts/run_facebook.sh`.
GitHub Actions runners use datacenter IPs, which Facebook challenges almost
immediately, so CI leaves this source disabled by default.

Use a throwaway Facebook account. Scraping Facebook is against Meta's terms of
service and the account carrying those cookies is the one at risk.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from .. import http
from ..config import env, fold
from ..models import Listing
from ..units import area_detalhada, area_to_ha, price_to_brl

log = logging.getLogger("terreno.sources.facebook")

NAME = "facebook"
RESOURCE = "apify_usd"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
APIFY_LIMITS = "https://api.apify.com/v2/users/me/limits"
DEFAULT_ACTOR = os.getenv("APIFY_FB_ACTOR", "apify~facebook-marketplace-scraper")

# The actor's real input schema, read from
# https://api.apify.com/v2/acts/apify~facebook-marketplace-scraper/builds/default
# (public, no token needed — re-read it before ever changing this payload):
#
#     required:  startUrls  (array of {"url": ...}), each matching
#                ^https?://www\.facebook\.com/marketplace/.*
#     optional:  resultsLimit (int, min 1), includeListingDetails (bool)
#
# The payload sent until 2026-08-11 (`search`/`maxItems`/`country`) matched none
# of those, which is why every call came back
# `400 Input is not valid: Field input.startUrls is required`. The error message
# names the offending field, so it is a cheap thing to re-derive rather than
# guess at.
MAX_START_URLS = int(os.getenv("APIFY_FB_MAX_URLS", "6"))

# Pay-per-result: the actor's README states $5 per 1000 items, i.e. $0.005 an
# item, and it is billed on items returned rather than on runs. So the cost of
# a run is bounded by `resultsLimit` and the ledger estimate has to be derived
# from it -- a fixed 0.15 under-counted a 40-item run by a third, which is how
# a $5/month credit gets overspent while the guard still reports headroom.
USD_POR_ITEM = 0.005
RESULTS_LIMIT = max(1, int(os.getenv("APIFY_FB_RESULTS", "30")))
EST_USD_PER_RUN = max(0.02, round(RESULTS_LIMIT * USD_POR_ITEM, 4))

# Descriptions are not a nice-to-have here: scoring reads water, area and
# building evidence out of the listing text (terreno/scoring.py), and a
# Marketplace card title alone almost never carries a hectare figure -- so
# without details most results would be filtered out as unparseable and the
# credit spent on them wasted. Costs more per item, hence the override.
INCLUDE_DETAILS = os.getenv("APIFY_FB_DETALHES", "1") not in ("0", "false", "False")


def fetch(criteria, store, budgets) -> list[Listing]:
    listings = _via_apify(criteria, store, budgets)
    if listings:
        return listings
    return _via_playwright(criteria, budgets)


# ------------------------------------------------------------------- Apify
def _via_apify(criteria, store, budgets) -> list[Listing]:
    token = env("APIFY_TOKEN")
    if not token:
        log.info("APIFY_TOKEN not set — skipping Apify backend")
        return []

    cap = float(budgets.get("apify_usd_por_mes", 5.0))
    if store.budget_remaining(RESOURCE, cap) < EST_USD_PER_RUN:
        log.warning("apify: monthly credit budget exhausted — skipping")
        return []

    limits = http.get_json(APIFY_LIMITS, params={"token": token}, retries=1)
    if limits:
        data = limits.get("data", {})
        used = (data.get("current") or {}).get("monthlyUsageUsd", 0)
        allowed = (data.get("limits") or {}).get("maxMonthlyUsageUsd", cap)
        if used and allowed and used >= allowed:
            log.warning("apify: account credit spent (%.2f/%.2f)", used, allowed)
            return []

    urls = _start_urls(criteria)
    if not urls:
        log.warning("apify: nenhuma URL de busca montada — pulando")
        return []

    # One actor call for every URL, not one per state: the actor takes the
    # whole list itself, so splitting it would multiply the per-run cost for
    # no extra coverage.
    payload = {
        "startUrls": [{"url": u} for u in urls],
        "resultsLimit": RESULTS_LIMIT,
        "includeListingDetails": INCLUDE_DETAILS,
    }
    log.info("apify: %d URL(s) de busca, limite=%d, detalhes=%s (~US$ %.2f)",
             len(urls), RESULTS_LIMIT, INCLUDE_DETAILS, EST_USD_PER_RUN)
    for u in urls:
        log.debug("apify: %s", u)

    items = _apify_post(DEFAULT_ACTOR, token, payload)
    store.budget_spend(RESOURCE, EST_USD_PER_RUN)
    items = [i for i in (items or []) if isinstance(i, dict)]

    # Cada item traz de volta a start URL que o produziu (`facebookUrl`), o que
    # é a única forma de saber qual localidade rendeu zero. Uma localidade
    # inexistente devolve página vazia, não erro, então sem esta contagem ela
    # ficaria para sempre na lista consumindo uma vaga de `MAX_START_URLS` sem
    # que nada dissesse isso.
    por_url = Counter(str(i.get("facebookUrl") or "?") for i in items)
    for u in urls:
        log.info("apify: %d item(ns) de %s", por_url.get(u, 0), u)

    uf_padrao = criteria.states[0] if criteria.states else ""
    out: list[Listing] = []
    descartados = 0
    for item in items:
        listing = _from_apify(item, uf_padrao)
        if listing:
            out.append(listing)
        else:
            descartados += 1

    if items and not out:
        # O modo de falha mais caro deste ator: a chamada é aceita, os itens
        # chegam, e o mapeamento não reconhece nenhum campo -- crédito gasto,
        # zero listings, nenhum erro em lugar nenhum. As chaves do primeiro
        # item são exatamente o que se precisa para consertar `_from_apify`.
        log.warning("apify: %d itens retornados e nenhum virou listing — "
                    "o formato do ator provavelmente mudou. Chaves do 1º item: %s",
                    len(items), ", ".join(sorted(items[0])[:25]))
    elif descartados:
        log.info("apify: %d item(ns) descartados (vendidos/pendentes ou sem URL de item)",
                 descartados)
    if INCLUDE_DETAILS and out and not any(item.description for item in out):
        # Sem descrição o scoring não tem como ler água, área nem benfeitoria,
        # então pagar por detalhes e não receber texto é dinheiro fora — e o
        # nome do campo de descrição não é documentado, só inferido.
        log.warning("apify: detalhes pedidos mas nenhum item trouxe descrição — "
                    "confira o nome do campo (tentados: %s)", ", ".join(CAMPOS_DESCRICAO))

    log.info("facebook/apify: %d listings", len(out))
    return out


CONSULTA = os.getenv("APIFY_FB_CONSULTA", "sitio chacara fazenda terreno rural")

# Marketplace só existe por localidade, e a localidade é uma *página de cidade*
# do Facebook: ou o slug dela ou o id numérico. Não é derivável do nome do
# município. Duas coisas foram medidas antes de escrever esta tabela:
#
#  1. `https://www.facebook.com/marketplace/search/?query=...`, sem cidade,
#     devolve HTTP 400 do próprio Facebook ("Sorry, something went wrong").
#     Era a primeira URL da lista e a única "que nunca é cortada", ou seja o
#     ator gastava crédito abrindo uma página de erro em todo run. Removida.
#     O README do ator também só documenta três formas, todas com cidade:
#     `/marketplace/<local>/`, `/marketplace/<local>/<categoria>` e
#     `/marketplace/<local>/search/?query=...`.
#  2. Município pequeno simplesmente não tem página de cidade no Marketplace.
#     `saojosedoscampos` e `saopaulo` estão indexados como "Buy and Sell in
#     ...", e Pindamonhangaba aparece pelo id `106437592726976`; Monteiro
#     Lobato, São Bento do Sapucaí e Santo Antônio do Pinhal não têm nada além
#     de grupos e páginas comuns. Slug derivado do nome ("monteirolobato")
#     portanto acerta as cidades grandes e falha exatamente nos municípios que
#     são o alvo -- e falha em silêncio, com uma página vazia em vez de erro.
#
# Daí a troca: buscar a partir dos polos da região, não de cada município. A
# busca do Marketplace já é por raio (a dezenas de km, mesma ordem do
# `raio_km: 60` dos critérios), então São José dos Campos e Pindamonhangaba
# cobrem Monteiro Lobato; e o recorte fino continua sendo feito depois, pelo
# filtro de município/raio do pipeline, que é onde ele é confiável.
#
# Tokens confirmados como páginas de cidade reais e indexadas do Marketplace:
LOCAIS_MARKETPLACE = {
    "sao jose dos campos": "saojosedoscampos",
    "pindamonhangaba": "106437592726976",
    "sao paulo": "saopaulo",
}
# Polos por região e, se a região não for conhecida, por UF.
POLOS_REGIAO = {
    "vale do paraiba": ["saojosedoscampos", "106437592726976"],
}
POLOS_UF = {
    "SP": ["saopaulo"],
}


def _locais(criteria) -> list[str]:
    """Tokens de localidade do Marketplace (slug ou id numérico) a consultar.

    `APIFY_FB_LOCAIS` (vírgula) é o jeito de acrescentar uma cidade sem mexer
    no código -- basta copiar o pedaço da URL do Marketplace dela.
    """
    override = env("APIFY_FB_LOCAIS")
    if override:
        return [t.strip().strip("/") for t in override.split(",") if t.strip()]

    tokens: list[str] = []
    sem_pagina: list[str] = []
    for municipio in criteria.municipalities:
        token = LOCAIS_MARKETPLACE.get(fold(municipio))
        if token:
            tokens.append(token)
        else:
            sem_pagina.append(municipio)

    polos = POLOS_REGIAO.get(fold(criteria.regiao or ""), [])
    tokens += [t for t in polos if t not in tokens]
    if not tokens:
        for uf in criteria.states:
            tokens += [t for t in POLOS_UF.get(uf, []) if t not in tokens]

    if sem_pagina:
        # Não é um aviso de erro: é o caso normal para município pequeno, e o
        # ponto é que o log diga em voz alta quem está sendo coberto por raio
        # em vez de por busca própria.
        log.info("apify: %d município(s) sem página de cidade no Marketplace "
                 "(%s) — cobertos pelo raio dos polos %s; APIFY_FB_LOCAIS "
                 "acrescenta uma cidade à mão",
                 len(sem_pagina), ", ".join(sem_pagina[:6]), ", ".join(tokens) or "(nenhum)")
    return tokens


def _start_urls(criteria) -> list[str]:
    """URLs de busca do Marketplace para o ator visitar.

    `APIFY_FB_START_URLS` (separadas por vírgula) substitui tudo isto — é a
    saída para quando o Facebook mudar o formato de novo, sem precisar de uma
    nova versão do código.
    """
    override = env("APIFY_FB_START_URLS")
    if override:
        return [u.strip() for u in override.split(",") if u.strip()][:MAX_START_URLS]

    # `exact=false` é o padrão do próprio Marketplace para uma busca de várias
    # palavras; min/maxPrice são os mesmos parâmetros que a UI dele põe na URL.
    # Filtrar preço na origem é o que faz o `resultsLimit` ser gasto em terra e
    # não em sofá usado -- os limites só entram quando são de fato limites.
    params: dict[str, str] = {"query": CONSULTA, "exact": "false"}
    if criteria.price_min > 0:
        params["minPrice"] = str(int(criteria.price_min))
    if 0 < criteria.price_max < 1e9:
        params["maxPrice"] = str(int(criteria.price_max))
    consulta = urlencode(params)

    urls = [
        f"https://www.facebook.com/marketplace/{token}/search/?{consulta}"
        for token in _locais(criteria)
    ]
    if len(urls) > MAX_START_URLS:
        log.info("apify: %d localidades, mas só cabem %d URLs "
                 "(ajuste APIFY_FB_MAX_URLS ou APIFY_FB_LOCAIS)",
                 len(urls), MAX_START_URLS)
    return urls[:MAX_START_URLS]


def _apify_post(actor: str, token: str, payload: dict):
    """run-sync-get-dataset-items needs POST; terreno.http is GET-only by
    design, so this is the one place that reaches for requests directly."""
    import requests

    try:
        r = requests.post(
            APIFY_RUN.format(actor=actor),
            params={"token": token, "timeout": 300},
            json=payload,
            timeout=320,
        )
        if r.status_code >= 400:
            log.warning("apify: HTTP %s — %s", r.status_code, r.text[:200])
            return []
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("apify: %s", exc)
        return []


# O item base do ator é o objeto GraphQL do Facebook quase cru — os nomes de
# campo abaixo estão em camelCase conforme retornado pelo ator real
# (conferidos na resposta de teste: `itemUrl`, `id`, `listingTitle`, `listingPrice`,
# `location`, `primaryListingPhoto`, `isSold`, `description`, `timestamp`, etc).
#
# O que `includeListingDetails` acrescenta não está documentado — o README só
# promete "description, location coordinates, time stamp, listing attributes".
# Daí a lista de candidatos: o resto do payload tenta variações de GraphQL,
# camelCase e snake_case. Se nenhuma pegar, `_via_apify`
# avisa em vez de devolver descrição vazia caladamente.
#
# `redacted_description` vem primeiro (2026-08-13): é o campo confirmado
# contra uma página real do Facebook contendo o texto de verdade escrito
# pelo vendedor (`{"text": "UMA linda área rural..."}`). Um genérico
# `description`, se o ator o fornecer, tende a ser sintetizado/categórico e
# perdeu contra o texto real numa comparação direta -- ver o card do
# "Estúdio 0 banheiros" no SESSION_NOTES, onde um `description` genérico
# (ou nenhum campo neste índice) produziu "riacho"/"casa" que a página real
# não tem em lugar nenhum.
CAMPOS_DESCRICAO = (
    "redacted_description", "description", "listingDescription",
    "marketplace_listing_description", "listing_description", "custom_title",
)


def _texto(valor) -> str:
    """String de um campo que pode vir crua ou embrulhada em `{"text": ...}`."""
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, dict):
        for chave in ("text", "value", "uri"):
            interno = valor.get(chave)
            if isinstance(interno, str) and interno.strip():
                return interno.strip()
    return ""


def _descricao(item: dict) -> str:
    for campo in CAMPOS_DESCRICAO:
        texto = _texto(item.get(campo))
        if texto:
            return texto
    return ""


def _preco(item: dict) -> float | None:
    """Preço em BRL. `amount` primeiro: é o valor de máquina ("350000.00"),
    sem o "R$" nem a separação de milhar do `formatted_amount`, então não
    depende de o parser acertar a localidade."""
    for campo in ("listingPrice", "listing_price", "min_listing_price", "max_listing_price", "price"):
        bruto = item.get(campo)
        if isinstance(bruto, dict):
            for chave in ("amount", "formatted_amount"):
                valor = price_to_brl(str(bruto.get(chave) or ""))
                if valor:
                    return valor
        elif bruto:
            valor = price_to_brl(str(bruto))
            if valor:
                return valor
    return None


def _numero(valor) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _data_publicacao(item: dict) -> str:
    """Data do anúncio como texto ISO. O GraphQL do Facebook dá `creation_time`
    em epoch de segundos, e um "1712000000" cru é o que apareceria no card."""
    for campo in ("timestamp", "creation_time", "createdAt", "created_time", "posted_at"):
        bruto = item.get(campo)
        if isinstance(bruto, (int, float)) and bruto > 0:
            segundos = bruto / 1000 if bruto > 1e11 else bruto  # ms ou s
            try:
                return datetime.fromtimestamp(segundos, UTC).isoformat(
                    timespec="seconds")
            except (OverflowError, OSError, ValueError):
                continue
        texto = _texto(bruto)
        if texto:
            return texto
    return ""


def _from_apify(item: dict, uf: str) -> Listing | None:
    url = _texto(item.get("itemUrl")) or _texto(item.get("listingUrl")) or _texto(item.get("url"))
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    # Só anúncio individual serve. O ator devolve a start URL em `facebookUrl`,
    # e uma página de busca entrando aqui como se fosse anúncio seria um item
    # inútil publicado no site — a checagem positiva é o que garante que o que
    # sai daqui é `/marketplace/item/<id>`.
    if "/marketplace/item/" not in url:
        log.debug("apify: item sem URL de anúncio, descartado: %r", url[:120])
        return None
    # `isSold`/`isPending` vêm no item base, sem custo de detalhe. Anúncio
    # vendido é ruído puro numa lista que quer ser curta.
    if item.get("isSold") or item.get("isPending") or item.get("isHidden"):
        return None

    title = _texto(item.get("listingTitle")) or _texto(item.get("marketplace_listing_title")) or _texto(item.get("title"))
    description = _descricao(item)

    location = item.get("location")
    location = location if isinstance(location, dict) else {}
    # `reverse_geocode_detailed` é o nome real confirmado contra uma página
    # do Facebook (2026-08-13) -- `reverse_geocode` (sem o `_detailed`) nunca
    # bateu com nada, e por isso a cidade nunca era lida: toda a localização
    # caía para a sigla de estado sozinha ("SP"), que é exatamente o bug
    # relatado ("Localização: SP" em todo card, sem município nenhum).
    geo = location.get("reverse_geocode_detailed") or location.get("reverse_geocode")
    geo = geo if isinstance(geo, dict) else {}
    municipality = _texto(geo.get("city")) or _texto(location.get("city"))
    if not municipality:
        pagina = geo.get("city_page")
        if isinstance(pagina, dict):
            municipality = _texto(pagina.get("display_name")).split(",")[0]

    # A UF do próprio anúncio, quando o Facebook a dá como sigla, vale mais que
    # a do recorte: é ela que resolve o alqueire (paulista 2,42 ha vs. mineiro
    # 4,84 ha) e um anúncio de MG caindo como SP tem a área pela metade.
    estado = _texto(geo.get("state"))
    uf_item = estado.upper() if re.fullmatch(r"[A-Za-z]{2}", estado) else uf

    area = area_detalhada(f"{title}\n{description}", uf_item)

    foto = item.get("primaryListingPhoto") or item.get("primary_listing_photo")
    imagem = ""
    if isinstance(foto, dict):
        imagem = _texto(foto.get("image")) or _texto(foto.get("uri"))
    if not imagem:
        imagem = _texto(item.get("image"))

    return Listing(
        source=NAME,
        source_id=_texto(item.get("id")) or url.rstrip("/").split("/")[-1],
        url=url,
        title=title,
        description=description,
        price=_preco(item),
        area_ha=area.ha,
        area_alqueires=area.alqueires,
        area_alqueire_tipo=area.alqueire_tipo,
        municipality=municipality,
        uf=uf_item,
        lat=_numero(location.get("latitude")),
        lon=_numero(location.get("longitude")),
        posted_at=_data_publicacao(item),
        image=imagem,
    )


# --------------------------------------------------------------- Playwright
def _via_playwright(criteria, budgets) -> list[Listing]:
    """Local fallback. Requires FB_COOKIES_FILE — a cookies JSON exported from
    the burner account's browser session."""
    cookies_file = env("FB_COOKIES_FILE")
    if not cookies_file or not Path(cookies_file).exists():
        log.info("FB_COOKIES_FILE not set or missing — skipping Playwright backend")
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed — skipping Playwright backend")
        return []

    out: list[Listing] = []
    with open(cookies_file, encoding="utf-8") as fh:
        cookies = json.load(fh)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="pt-BR",
            user_agent=http.UA,
            viewport={"width": 1280, "height": 900},
        )
        context.add_cookies(cookies)
        page = context.new_page()

        for uf in criteria.states:
            query = f"terreno chácara sítio {uf}"
            url = ("https://www.facebook.com/marketplace/category/propertyforsale"
                   f"?query={query}&exact=false")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                if "login" in page.url:
                    log.error("facebook: cookies rejected — session expired")
                    break
                for _ in range(3):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1500)
                out.extend(_scrape_cards(page, uf))
            except Exception as exc:  # noqa: BLE001
                log.warning("facebook: %s", exc)
        browser.close()

    log.info("facebook/playwright: %d listings", len(out))
    return out


def _scrape_cards(page, uf: str) -> list[Listing]:
    cards = page.query_selector_all('a[href*="/marketplace/item/"]')
    out: list[Listing] = []
    seen: set[str] = set()
    for card in cards:
        href = (card.get_attribute("href") or "").split("?")[0]
        if not href or href in seen:
            continue
        seen.add(href)
        text = card.inner_text() or ""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        title = next((line for line in lines if "R$" not in line), "")
        price_line = next((line for line in lines if "R$" in line), "")
        out.append(Listing(
            source=NAME,
            url="https://www.facebook.com" + href,
            source_id=href.rstrip("/").split("/")[-1],
            title=title,
            description=text,
            price=price_to_brl(price_line),
            area_ha=area_to_ha(text, uf),
            municipality=lines[-1] if lines else "",
            uf=uf,
        ))
    return out
