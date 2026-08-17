"""No-API discovery for `sites_descobertos` hosts classified as `imobiliaria`
(see `terreno/site_categoria.py`).

Everything Layer B (`brave_discover.py`/`brave_visit.py`) does for these hosts
today is: spend a metered Brave query on `site:<host> ...` to ask a search
engine what that host has for sale. But we already *know* the host -- an
imobiliária's own site is the authoritative, always-current list of what it
has for sale, and real-estate sites overwhelmingly expose that list through
one of two structures a crawler can exploit directly, at zero API cost:

  1. A sitemap (`/sitemap.xml`, often an index of per-category sitemaps) --
     most SEO-conscious sites keep one exactly so search engines find their
     listings without being asked; we can just read it the same way.
  2. Failing that, the homepage's own navigation -- an internal link whose
     text or URL slug says "sítio"/"fazenda"/"chácara"/"rural"/"venda" is
     almost always a category or listing page, not noise.

Every URL this turns up still goes through the same `extract.rules.extract()`
as a Brave-found page -- this module only replaces *how the URL is found*, not
how it's judged.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from .. import http
from ..extract import rules
from ..models import Listing
from ..site_categoria import IMOBILIARIA

log = logging.getLogger("terreno.sources.imobiliaria_crawl")

NAME = "imobiliaria_crawl"

# Anything containing one of these (case-/accent-folded) in its path or link
# text is worth a look; a link to "quem somos" or "contato" is not.
_PALAVRAS_RURAIS = (
    "sitio", "fazenda", "chacara", "rural", "terreno", "venda",
    "propriedade", "imovel", "imoveis",
)

_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _fold(s: str) -> str:
    return (s or "").lower()


def _relevante(url: str, texto: str = "") -> bool:
    alvo = _fold(url) + " " + _fold(texto)
    return any(p in alvo for p in _PALAVRAS_RURAIS)


def _sitemap_urls(base: str, max_urls: int) -> list[str]:
    """Reads /sitemap.xml (or a sitemap index one level deep), returns
    listing-shaped URLs. A sitemap is XML, not HTML, so a dedicated tiny
    parser instead of reusing the HTML-link regex below."""
    for path in _SITEMAP_PATHS:
        resp = http.get(urljoin(base, path), timeout=20, retries=1)
        if resp is None:
            continue
        parece_xml = ("xml" in resp.headers.get("content-type", "")
                      or "<urlset" in resp.text[:200] or "<sitemapindex" in resp.text[:200])
        if not parece_xml:
            continue
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError:
            continue
        tag = root.tag.rsplit("}", 1)[-1]
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        if tag == "sitemapindex":
            # One level of recursion only -- a sitemap-of-sitemaps' children
            # are themselves full sitemaps, not worth chasing indefinitely.
            achados: list[str] = []
            for sub in locs[:10]:
                r2 = http.get(sub, timeout=20, retries=1)
                if r2 is None:
                    continue
                try:
                    root2 = ElementTree.fromstring(r2.content)
                except ElementTree.ParseError:
                    continue
                achados.extend(el.text.strip() for el in root2.iter()
                               if el.tag.endswith("loc") and el.text)
                if len(achados) >= max_urls * 4:
                    break
            locs = achados
        return [u for u in locs if _relevante(u)][:max_urls]
    return []


def _links_da_pagina(base: str, html: str, max_links: int) -> list[str]:
    host = urlparse(base).netloc
    vistos: set[str] = set()
    out: list[str] = []
    for href, texto in _LINK_RE.findall(html):
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href)
        if urlparse(full).netloc != host or full in vistos:
            continue
        vistos.add(full)
        if _relevante(full, re.sub(r"<[^>]+>", " ", texto)):
            out.append(full)
        if len(out) >= max_links:
            break
    return out


def crawl_host(host: str, *, max_candidatos: int = 40, timeout: int = 25) -> list[str]:
    """Every candidate URL found on `host` worth trying as a listing page --
    no search API, no query cost. Sitemap first (cheap, one or two requests,
    usually complete); homepage link-following only when there is no usable
    sitemap."""
    base = f"https://{host}/"
    candidatos = _sitemap_urls(base, max_candidatos)
    if candidatos:
        log.debug("%s: %d candidato(s) via sitemap", host, len(candidatos))
        return candidatos

    resp = http.get(base, timeout=timeout, retries=2)
    if resp is None:
        return []
    candidatos = _links_da_pagina(base, resp.text, max_candidatos)
    log.debug("%s: %d candidato(s) via homepage (sem sitemap útil)", host, len(candidatos))
    return candidatos


def fetch_host(host: str, *, budgets: dict | None = None,
                already: frozenset[str] = frozenset()) -> list[Listing]:
    """Crawl `host` and extract whatever the candidate pages actually are.
    `already` (URLs the results DB already has) skips a page worth nothing
    new -- same convention every other source in this pipeline uses before
    spending an HTTP request on a URL it has already stored."""
    budgets = budgets or {}
    max_candidatos = int(budgets.get("imobiliaria_max_candidatos", 40))
    timeout = int(budgets.get("imobiliaria_timeout_pagina_s", 25))

    urls = crawl_host(host, max_candidatos=max_candidatos, timeout=timeout)
    out: list[Listing] = []
    for url in urls:
        if url in already:
            continue
        resp = http.get(url, timeout=timeout, retries=1)
        if resp is None:
            continue
        listing = rules.extract(resp.text, url, source=NAME)
        if listing:
            out.append(listing)
    return out


def fetch(criteria, store, budgets) -> list[Listing]:
    """Twice-weekly full sweep of every promoted `imobiliaria` SDB host --
    no weekly-due gate (see `Store.sites_descobertos_hosts`), since this
    strategy has no metered cost to ration against. Runs as its own
    scheduled job (source name `imobiliaria_crawl`, see
    `search_crawl_imobiliaria.yml`) alongside the daily Tavily-driven scan,
    not instead of it: a real benchmark (2026-08-17,
    scripts/diagnostico_imobiliaria_crawl.py) showed Tavily wins on average
    but this crawler still finds real listings on hosts Tavily's search-based
    approach misses entirely -- belt and suspenders.

    Hosts run in parallel (network-bound, same reasoning as
    `brave_visit.visit_all`); each host's own candidate pages are still
    fetched one at a time internally (`fetch_host`), since a single host's
    own candidate count is small enough not to need its own pool.
    """
    hosts = store.sites_descobertos_hosts(IMOBILIARIA)
    if not hosts:
        log.info("imobiliaria_crawl: nenhum host imobiliária promovido na SDB")
        return []

    already = frozenset(store.seen_urls())
    paralelismo = int(budgets.get("imobiliaria_paralelismo", 20))
    log.info("imobiliaria_crawl: varrendo %d host(s) imobiliária (paralelismo %d)",
             len(hosts), paralelismo)

    out: list[Listing] = []
    with ThreadPoolExecutor(max_workers=min(paralelismo, len(hosts))) as pool:
        futuros = {pool.submit(fetch_host, h, budgets=budgets, already=already): h for h in hosts}
        for futuro in as_completed(futuros):
            host = futuros[futuro]
            try:
                out.extend(futuro.result())
            except Exception as exc:  # noqa: BLE001 — um host ruim não pode derrubar a varredura
                log.debug("imobiliaria_crawl: falha em %s: %s", host, exc)

    log.info("imobiliaria_crawl: %d listing(s) extraído(s) de %d host(s)", len(out), len(hosts))
    return out
