"""OLX — the largest classifieds source for rural land in Brazil.

OLX has no bot-wall (a plain curl_cffi fetch gets a clean 200) -- the actual
problem is that its search-results page moved to Next.js App Router, which
streams listing data client-side via `self.__next_f.push(...)` (an internal
React format) instead of the classic `__NEXT_DATA__` script tag a plain HTTP
fetch could read. A real headless browser that waits for hydration sees the
same rendered links a human does; see scripts/diagnostico_olx_navegador.py,
which proved out this exact approach (`domcontentloaded` + a fixed wait, then
harvest `<a href>` values), and SESSION_NOTES.md for the full investigation.

Individual listing URLs point at a state-specific subdomain
(`sp.olx.com.br`, not `www.olx.com.br`) and carry price/area/id in the slug
itself, e.g.:

    https://sp.olx.com.br/vale-do-paraiba-e-litoral-norte/terrenos/
        terreno-a-venda-1000-m-por-r-350-000-00-village-da-serra-tremembe-sp-
        ref-te0635-1526137608

Reading price/area straight out of that slug -- same philosophy as
htmlportal.py::_from_slug -- means only the listings that survive filtering
ever cost a detail-page fetch (done later, generically, by
pipeline.enrich()). Municipality is deliberately left for enrich() to fill
in, exactly like the chavesnamao slug parser: the region segment of the URL
path (e.g. "vale-do-paraiba-e-litoral-norte") is a marketing zone, not a
municipality, and guessing one out of the free-text title slug is not worth
the false positives.
"""

from __future__ import annotations

import logging
import re

from .. import http
from ..models import Listing
from ..units import area_to_ha
from .base import UF_NAMES

log = logging.getLogger("terreno.sources.olx")

NAME = "olx"
BASE = "https://www.olx.com.br/imoveis/terrenos"

# A real listing link after hydration: a state subdomain (sp.olx.com.br, not
# www), any path, ending in a 6+ digit id -- the most stable pattern any
# classifieds portal has, confirmed against real rendered output in
# scripts/diagnostico_olx_navegador.py.
# `www` is accepted too (2026-10-30): some cards link the ad on the bare domain
# ("/terrenos/...-1543914631"); the 6+ digit id keeps category links out.
_LISTING_HREF_RE = re.compile(
    r"^https?://[a-z]{2,3}\.olx\.com\.br/.+-(\d{6,})/?(?:\?.*)?$", re.I
)

# "...-1000-m-por-r-350-000-00-village-..." -- reais are dash-separated
# digit groups right after "por-r-", the last group being centavos.
_PRICE_RE = re.compile(r"por-r-(\d+(?:-\d+)*)", re.I)

_NAV_TIMEOUT_MS = 30_000
# "networkidle" never resolves on this site (ads/telemetry keep the network
# busy forever, measured as a pure timeout) -- domcontentloaded + a fixed
# wait for hydration is the condition that actually matches reality here.
_HYDRATION_WAIT_MS = 6_000

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def _novo_contexto(browser):
    """A context that looks like the Chromium it really is. The old Safari/macOS
    user agent on a Linux Chromium is a fingerprint mismatch, and Playwright's
    own default says "HeadlessChrome" -- since 2026-10-30 OLX answers either with
    a Cloudflare "Attention Required" page."""
    major = (browser.version or "").split(".")[0] or "130"
    ua = (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{major}.0.0.0 Safari/537.36")
    ctx = browser.new_context(user_agent=ua, locale="pt-BR",
                              timezone_id="America/Sao_Paulo",
                              viewport={"width": 1366, "height": 900})
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return ctx


def _bloqueada(titulo: str) -> bool:
    t = (titulo or "").lower()
    return any(x in t for x in ("attention required", "just a moment", "access denied",
                                "um momento"))


_CARD_SPLIT = re.compile(r'<section[^>]*class="[^"]*olx-adcard\b', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_LOCAL_RE = re.compile(
    r'class="[^"]*(?:location|address|locality)[^"]*"[^>]*>\s*([^<]+)', re.I)


def _texto_do_bloco(bloco: str) -> str:
    bloco = re.sub(r"<(?:svg|script|style)\b.*?</(?:svg|script|style)>", " ", bloco,
                   flags=re.S | re.I)
    bloco = re.sub(r"<(?:br|/div|/p|/span|/h\d|/li|/section)[^>]*>", "\n", bloco, flags=re.I)
    bloco = re.sub(r"<[^>]+>", "", bloco).replace("&amp;", "&").replace("&nbsp;", " ")
    bloco = re.sub(r"[ \t]+", " ", bloco)
    return "\n".join(l.strip() for l in bloco.splitlines() if l.strip())


def _cards_do_html(html: str, base: str) -> list[tuple[str, str]]:
    """Same [(href, text)] the browser path returns, read from fetched HTML --
    the fallback when the browser is walled off but the HTTP ladder (curl_cffi,
    then the unblocker) gets through.

    Measured 2026-10-30: a card is a `<section class="olx-adcard">` whose link
    holds only the title; the area, price and place are *siblings* of the link,
    inside the section -- so the card text is the whole section, not the
    anchor's."""
    from urllib.parse import urljoin
    out = []
    partes = _CARD_SPLIT.split(html or "")
    for bloco in partes[1:]:
        bloco = bloco.split(">", 1)[-1]          # rest of the <section ...> tag
        href = next((urljoin(base, h.replace("&amp;", "&"))
                     for h in _HREF_RE.findall(bloco)
                     if _LISTING_HREF_RE.match(urljoin(base, h.replace("&amp;", "&")))), None)
        if not href:
            continue
        texto = _texto_do_bloco(bloco)
        local = _LOCAL_RE.search(bloco)
        if local:
            # place named by its own element: put it right after the price line
            texto += "\n" + local.group(1).strip()
        out.append((href, texto))
    return out


def _price_from_slug(slug: str) -> float | None:
    m = _PRICE_RE.search(slug)
    if not m:
        return None
    parts = m.group(1).split("-")
    if len(parts) >= 2 and len(parts[-1]) == 2:
        reais, cents = "".join(parts[:-1]), parts[-1]
    else:
        reais, cents = "".join(parts), "00"
    if not reais:
        return None
    try:
        value = float(f"{reais}.{cents}")
    except ValueError:
        return None
    return value if value > 0 else None


def _title_from_slug(slug: str, listing_id: str) -> str:
    slug = re.sub(rf"-{re.escape(listing_id)}/?$", "", slug)
    words = [w for w in slug.split("-") if w]
    title = re.sub(r"\ba venda\b", "à venda", " ".join(words)).strip()
    return title.capitalize()


# The result card always shows the price ("R$ 110.000"), but most listing slugs
# do not carry it ("por-r-..." is the exception) -- so reading it from the slug
# alone left almost every OLX listing without a price, which then slipped
# through any price ceiling as "unknown passes" (found 2026-10-13: R$120k
# section showing OLX plots that cost more). The card text is read in the page
# itself: the anchor's own text, or the nearest ancestor that has a price and
# holds only this one listing link.
_CARD_JS = """els => els.map(a => {
  let n = a, t = a.innerText || '';
  for (let i = 0; i < 4 && !/R\\$/.test(t) && n.parentElement; i++) {
    n = n.parentElement;
    // a card links the same ad twice (photo + title): count distinct hrefs
    const hs = new Set([...n.querySelectorAll('a[href*="olx.com.br"]')]
      .map(x => x.getAttribute('href')));
    if (hs.size > 1) break;
    t = n.innerText || '';
  }
  return [a.href, t.slice(0, 400)];
})"""
_CARD_PRICE_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?")


def _price_from_card(text: str) -> float | None:
    m = _CARD_PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(".", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _municipio_from_card(text: str) -> str:
    """Card layout (measured 2026-10-13): "título / 489m² / R$ 1.500 / [IPTU..] /
    Sorocaba, Quintais do Imperador / Hoje, 18:46". The município is the first
    comma-separated piece of the first line after the price lines."""
    linhas = [l.strip() for l in (text or "").splitlines() if l.strip()]
    ultimo = None
    for i, l in enumerate(linhas):
        if "R$" in l:
            ultimo = i
    if ultimo is None:
        return ""
    for l in linhas[ultimo + 1:]:
        if re.match(r"^(iptu|condom[ií]nio)", l, re.I):
            continue
        if ":" in l or l.split(",")[0].strip().lower() in ("hoje", "ontem", "chat"):
            return ""
        muni = l.split(",")[0].strip()
        return "" if re.search(r"\d", muni) else muni
    return ""


def _area_from_card(text: str) -> str:
    m = re.search(r"^\s*(\d[\d.]*)\s*m²\s*$", text or "", re.M)
    return f"{m.group(1)} m²" if m else ""


def _listing_from_url(url: str, uf: str) -> Listing | None:
    m = _LISTING_HREF_RE.match(url.strip())
    if not m:
        return None
    listing_id = m.group(1)
    slug = url.rsplit("/", 1)[-1].split("?")[0]
    return Listing(
        source=NAME,
        source_id=listing_id,
        url=url,
        title=_title_from_slug(slug, listing_id),
        price=_price_from_slug(slug),
        area_ha=area_to_ha(slug.replace("-", " "), uf),
        uf=uf,
    )


def fetch_html(urls: list[str], *, espera_ms: int = 2_500) -> dict[str, str]:
    """Detail pages through the same real headless browser the search pages
    already pass with.

    OLX's search pages load fine in headless Chromium, but the plain-HTTP
    ladder (requests, curl_cffi impersonation, even the paid unblocker) gets
    403 or an unreadable page on the *detail* pages -- measured 2026-10-13: the
    unblocker's 15-request budget was spent on OLX alone and most listings
    still came back without price, município or description. A browser is
    what actually passes, so detail pages go through one. One browser, one
    page, sequential and unhurried (one page per ~4 s is also what keeps this
    looking like a person); {url: html} for what loaded, nothing for what
    didn't -- a failure must not take the run down.
    """
    out: dict[str, str] = {}
    if not urls:
        return out
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("olx: playwright não instalado, sem páginas de detalhe")
        return out
    bloqueadas = seguidas = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = _novo_contexto(browser).new_page()
                for url in urls:
                    try:
                        resp = page.goto(url, timeout=_NAV_TIMEOUT_MS,
                                         wait_until="domcontentloaded")
                        page.wait_for_timeout(espera_ms)
                        if resp is not None and resp.status >= 400:
                            bloqueadas += 1
                            seguidas += 1
                            # Measured 2026-10-13: 119 of 120 refused from the
                            # CI runner. If the first few all are, the rest
                            # will be too -- stop paying ~4 s each for it.
                            if seguidas >= 6 and not out:
                                log.warning("olx: detalhes recusados pelo navegador "
                                            "(%d seguidos) -- desistindo", seguidas)
                                break
                            continue
                        seguidas = 0
                        out[url] = page.content()
                    except Exception as exc:  # noqa: BLE001 -- one page must not stop the rest
                        log.debug("olx: detalhe falhou %s: %s", url, exc)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- browser failure must not take the run down
        log.warning("olx: erro no navegador (detalhes): %s", exc)
    log.info("olx: %d de %d páginas de detalhe lidas pelo navegador (%d recusadas)",
             len(out), len(urls), bloqueadas)
    return out


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_paginas_por_fonte", 5))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("olx: playwright não instalado, pulando fonte")
        return out

    seen_urls: set[str] = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = _novo_contexto(browser).new_page()
                for uf in criteria.states:
                    if not UF_NAMES.get(uf):
                        continue
                    for pageno in range(1, max_pages + 1):
                        params = {"o": pageno}
                        if criteria.price_min:
                            params["ps"] = int(criteria.price_min)
                        if criteria.price_max:
                            params["pe"] = int(criteria.price_max)
                        qs = "&".join(f"{k}={v}" for k, v in params.items())
                        url = f"{BASE}/estado-{uf.lower()}?{qs}"

                        try:
                            page.goto(url, timeout=_NAV_TIMEOUT_MS,
                                       wait_until="domcontentloaded")
                        except Exception as exc:  # noqa: BLE001 -- a source must degrade, not take the run down
                            log.warning("olx: falha ao carregar %s p%d: %s",
                                        uf, pageno, exc)
                            break

                        page.wait_for_timeout(_HYDRATION_WAIT_MS)
                        titulo = page.title() or ""
                        if _bloqueada(titulo):
                            log.warning("olx %s p%d: navegador barrado (%r)", uf, pageno,
                                        titulo[:50])
                            cards = []
                        else:
                            # Cards below the fold render lazily: without
                            # scrolling they come back as empty text.
                            for _ in range(6):
                                page.mouse.wheel(0, 2500)
                                page.wait_for_timeout(500)
                            cards = page.eval_on_selector_all("a", _CARD_JS)
                        if not any(_LISTING_HREF_RE.match(h or "") for h, _ in cards or []):
                            # Walled off (or nothing rendered): the HTTP ladder
                            # (curl_cffi, then the paid unblocker) as a fallback,
                            # unblocker credits are shared with every other source in the run.
                            # (curl_cffi gets through for free -- measured 2026-10-30,
                            # 150 of 150 cards with price -- so every page uses it; the
                            # per-run cap on the paid unblocker bounds the rest.)
                            resp = http.get(url, timeout=60, retries=1)
                            if resp is not None:
                                cards = _cards_do_html(resp.text, url)
                                log.info("olx %s p%d: via HTTP, %d anúncio(s)", uf,
                                         pageno, len(cards))

                        # A card links the same ad twice (photo, title); the photo
                        # link has no text. Keep the richest text per ad instead of
                        # the first one seen.
                        melhor: dict[str, str] = {}
                        for href, texto in cards or []:
                            if not href:
                                continue
                            antigo = melhor.get(href)
                            if antigo is None or ("R$" in texto and "R$" not in antigo):
                                melhor[href] = texto or ""
                        if pageno == 1:
                            log.info("olx %s p1: %d links, %d de anúncio, título %r",
                                     uf, len(cards or []),
                                     sum(1 for h in melhor if _LISTING_HREF_RE.match(h)),
                                     titulo[:60])

                        new_this_page = 0
                        for href, texto in melhor.items():
                            if href in seen_urls:
                                continue
                            listing = _listing_from_url(href, uf)
                            if not listing:
                                continue
                            if listing.price is None:
                                listing.price = _price_from_card(texto)
                            if not listing.municipality:
                                listing.municipality = _municipio_from_card(texto)
                            if not listing.area_ha:
                                a = _area_from_card(texto)
                                if a:
                                    listing.area_ha = area_to_ha(a, uf)
                            seen_urls.add(href)
                            out.append(listing)
                            new_this_page += 1

                        if new_this_page == 0:
                            break  # last page, or nothing rendered
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- Playwright/browser failure must not take the run down
        log.warning("olx: erro no navegador: %s", exc)

    log.info("olx: %d listings (%d com preço, %d com município)", len(out),
             sum(1 for x in out if x.price), sum(1 for x in out if x.municipality))
    return out
