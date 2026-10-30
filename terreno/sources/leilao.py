"""Leilões (2026-10-30): imóveis rurais em leilão, de sites listados em
`data/leiloes.yaml`.

É uma infraestrutura própria, separada da dos anúncios de venda: a lista de
sites vive num arquivo de dados (um agente agendado acrescenta sites novos
nele, sem tocar em código), cada site é varrido por conta própria e tudo que
sai daqui é marcado `finalidade="leilao"` -- o que o leva para a seção Leilão
(rubrica de 6 estrelas, `scoring.avaliar_leilao`) e o mantém fora das seções de
venda e de aluguel.

Como cada site é varrido:
  1. Páginas de listagem semeadas no YAML (`paginas`, ou `paginas_por_municipio`
     expandida para os municípios do perímetro) -- é o caminho preciso.
  2. Sem sementes, sitemap/navegação do host (o mesmo método do crawler de
     imobiliárias), ficando só com links que parecem imóvel rural.
Cada link candidato passa pelo extrator genérico (`extract.rules.extract`);
o preço é então corrigido para o *lance* (1ª/2ª praça, lance mínimo) quando o
texto o traz, já que o primeiro "R$" de uma página de leilão costuma ser a
avaliação.

Nenhuma API paga. Um site que bloqueia ou muda vira zero lotes e uma linha no
log (`leilao: <site>: N candidatos, M lotes`), não derruba a execução.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import yaml

from .. import http
from ..config import ROOT, fold
from ..extract import rules
from ..models import Listing
from . import imobiliaria_crawl

log = logging.getLogger("terreno.sources.leilao")

NAME = "leilao"
LEILOES_PATH = ROOT / "data" / "leiloes.yaml"

# Municípios do perímetro que ficam em Minas (o resto é SP); só serve para
# montar a URL por cidade dos sites que têm página por município.
_MG = {fold(m) for m in (
    "Paraisópolis", "Consolação", "Gonçalves", "Sapucaí-Mirim", "Brazópolis",
    "Cruz Vera", "Wenceslau Braz", "Camanducaia")}

# Lote que parece terra; e o que nunca é imóvel (veículos, máquinas...).
_RURAL = ("rural", "sitio", "fazenda", "chacara", "gleba", "terra", "terreno",
          "haras", "propriedade", "area", "imovel", "imoveis")
_NAO_IMOVEL = ("veiculo", "carro", "moto", "caminhao", "onibus", "maquina",
               "sucata", "trator", "embarcacao", "joia", "eletronico", "mobiliario",
               "apartamento", "sala-comercial", "loja")

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\']', re.I)

_PRECO_LEILAO_RE = re.compile(
    r"(?:lance\s+(?:inicial|m[ií]nimo)|valor\s+m[ií]nimo|[12]\s*[ªaº°]?\s*pra[cç]a|"
    r"1[ªº]\s*leil[aã]o|2[ªº]\s*leil[aã]o)[^R$]{0,60}R\$\s*([\d.]+(?:,\d{1,2})?)", re.I)


def carregar_sites(path: Path = LEILOES_PATH) -> list[dict]:
    try:
        dados = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("leilao: %s ilegível: %s", path.name, exc)
        return []
    return [s for s in (dados.get("sites") or [])
            if isinstance(s, dict) and s.get("host") and s.get("ativo", True)]


def _slug(nome: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(nome)).strip("-")


def _paginas(site: dict, municipios: list[str]) -> list[str]:
    paginas = list(site.get("paginas") or [])
    modelo = site.get("paginas_por_municipio")
    if modelo:
        for m in municipios:
            uf = "mg" if fold(m) in _MG else "sp"
            paginas.append(modelo.format(slug=_slug(m), uf=uf, UF=uf.upper(),
                                         municipio=m, municipio_url=quote(m)))
    # `paginar: {param: pagina, ate: 6}` -> each seed page also as ?pagina=2..6
    pag = site.get("paginar")
    if pag and paginas:
        param, ate = pag.get("param", "pagina"), int(pag.get("ate", 3))
        base = list(paginas)
        for p in base:
            sep = "&" if "?" in p else "?"
            paginas += [f"{p}{sep}{param}={n}" for n in range(2, ate + 1)]
    return paginas


def _parece_lote(url: str, padrao: re.Pattern | None) -> bool:
    if padrao is not None:
        return bool(padrao.search(url))
    alvo = fold(url)
    return (any(p in alvo for p in _RURAL)
            and not any(p in alvo for p in _NAO_IMOVEL))


def _links(base: str, html: str, host: str) -> list[str]:
    vistos: dict[str, None] = {}
    for href in _LINK_RE.findall(html):
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href).split("#")[0]
        if urlparse(full).netloc.endswith(host.removeprefix("www.")):
            vistos.setdefault(full, None)
    return list(vistos)


def candidatos(site: dict, municipios: list[str], *, maximo: int, timeout: int) -> list[str]:
    host = site["host"]
    padrao = re.compile(site["padrao_anuncio"]) if site.get("padrao_anuncio") else None
    achados: dict[str, None] = {}
    for pagina in _paginas(site, municipios):
        resp = http.get(pagina, timeout=timeout, retries=1)
        if resp is None:
            continue
        for link in _links(pagina, resp.text, host):
            if link != pagina and _parece_lote(link, padrao):
                achados.setdefault(link, None)
        if len(achados) >= maximo:
            break
    if not achados and not _paginas(site, municipios):
        for link in imobiliaria_crawl.crawl_host(host, max_candidatos=maximo * 3,
                                                 timeout=timeout):
            if _parece_lote(link, None):
                achados.setdefault(link, None)
    return list(achados)[:maximo]


def _sem_menu(html: str) -> str:
    """The page without its site chrome. Found in the 2026-10-30 dry-run: the
    description of a Mega Leilões lot began with the whole category menu
    ("Imóveis Apartamentos Casas Hospitais ... Barcos Cavalos"), so a rural lot
    read as an apartment and every water/building keyword was noise."""
    for tag in ("nav", "header", "footer", "form", "select", "aside", "noscript"):
        html = re.sub(rf"<{tag}\b.*?</{tag}>", " ", html, flags=re.S | re.I)
    return html


def preco_do_lance(texto: str) -> float | None:
    """The auction's own price (lance mínimo / 1ª-2ª praça) from the page text:
    the lowest one stated, since the next round is the one that is open."""
    valores = []
    for bruto in _PRECO_LEILAO_RE.findall(texto or ""):
        try:
            v = float(bruto.replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if v > 0:
            valores.append(v)
    return min(valores) if valores else None


def fetch_site(site: dict, municipios: list[str], *, budgets: dict,
               ja_vistos: frozenset[str]) -> tuple[list[Listing], int]:
    maximo = int(budgets.get("leilao_max_candidatos", 60))
    timeout = int(budgets.get("leilao_timeout_pagina_s", 25))
    urls = candidatos(site, municipios, maximo=maximo, timeout=timeout)
    out: list[Listing] = []
    for url in urls:
        if url in ja_vistos:
            continue
        resp = http.get(url, timeout=timeout, retries=1)
        if resp is None:
            continue
        listing = rules.extract(_sem_menu(resp.text), url, source=NAME)
        if not listing:
            continue
        lance = preco_do_lance(f"{listing.title} {listing.description}")
        if lance:
            listing.price = lance
        listing.finalidade = "leilao"
        log.debug("leilao: %s | %s/%s | R$ %s | %s ha", listing.title[:60],
                  listing.municipality, listing.uf, listing.price, listing.area_ha)
        listing.description = f"{listing.description} · Leiloeiro: {site.get('nome', site['host'])}".strip(" ·")
        out.append(listing)
    return out, len(urls)


def fetch(criteria, store, budgets) -> list[Listing]:
    sites = carregar_sites()
    if not sites:
        log.info("leilao: nenhum site ativo em %s", LEILOES_PATH.name)
        return []
    municipios: list[str] = []
    for _, crit in criteria.secoes():
        if crit.finalidade == "leilao":
            municipios = crit.municipalities
    ja_vistos = frozenset(store.seen_urls())
    paralelismo = int(budgets.get("leilao_paralelismo", 8))
    log.info("leilao: %d site(s), %d município(s) no perímetro", len(sites), len(municipios))

    out: list[Listing] = []
    with ThreadPoolExecutor(max_workers=min(paralelismo, len(sites))) as pool:
        futuros = {pool.submit(fetch_site, s, municipios, budgets=budgets,
                               ja_vistos=ja_vistos): s for s in sites}
        for futuro in as_completed(futuros):
            site = futuros[futuro]
            try:
                lotes, n = futuro.result()
            except Exception as exc:  # noqa: BLE001 -- um site ruim não derruba a varredura
                log.warning("leilao: %s falhou: %s", site["host"], exc)
                continue
            log.info("leilao: %s: %d candidato(s), %d lote(s)", site["host"], n, len(lotes))
            for lote in lotes[:6]:
                log.info("leilao:   %s | %s/%s | R$ %s | %s ha", (lote.title or "")[:55],
                         lote.municipality or "?", lote.uf or "?", lote.price, lote.area_ha)
            out.extend(lotes)
    log.info("leilao: %d lote(s) de %d site(s)", len(out), len(sites))
    return out
