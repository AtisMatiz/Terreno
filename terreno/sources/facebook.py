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
from pathlib import Path

from .. import http
from ..config import env
from ..models import Listing
from ..units import area_to_ha, price_to_brl

log = logging.getLogger("terreno.sources.facebook")

NAME = "facebook"
RESOURCE = "apify_usd"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
APIFY_LIMITS = "https://api.apify.com/v2/users/me/limits"
DEFAULT_ACTOR = os.getenv("APIFY_FB_ACTOR", "apify~facebook-marketplace-scraper")
# Conservative estimate of actor cost per run, used to pre-check the ledger.
EST_USD_PER_RUN = 0.15


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

    cap = float(budgets.get("apify_usd_per_month", 5.0))
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

    out: list[Listing] = []
    for uf in criteria.states:
        payload = {
            "search": f"terreno chácara sítio {uf}",
            "maxItems": 40,
            "country": "BR",
        }
        items = _apify_post(DEFAULT_ACTOR, token, payload)
        store.budget_spend(RESOURCE, EST_USD_PER_RUN)
        for item in items or []:
            listing = _from_apify(item, uf)
            if listing:
                out.append(listing)

    log.info("facebook/apify: %d listings", len(out))
    return out


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


def _from_apify(item: dict, uf: str) -> Listing | None:
    url = item.get("listingUrl") or item.get("url") or ""
    if not url:
        return None
    title = item.get("marketplace_listing_title") or item.get("title") or ""
    description = item.get("description") or item.get("custom_title") or ""
    price_raw = item.get("listing_price") or item.get("price") or ""
    if isinstance(price_raw, dict):
        price_raw = price_raw.get("formatted_amount") or price_raw.get("amount") or ""

    location = item.get("location") or {}
    if isinstance(location, dict):
        municipality = (location.get("reverse_geocode") or {}).get("city", "") \
            or location.get("city", "")
    else:
        municipality = str(location)

    return Listing(
        source=NAME,
        source_id=str(item.get("id") or item.get("listingId") or ""),
        url=url,
        title=title,
        description=description,
        price=price_to_brl(str(price_raw)),
        area_ha=area_to_ha(f"{title} {description}", uf),
        municipality=municipality,
        uf=uf,
        image=item.get("primary_listing_photo", {}).get("image", {}).get("uri", "")
        if isinstance(item.get("primary_listing_photo"), dict) else item.get("image", ""),
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
    with open(cookies_file, "r", encoding="utf-8") as fh:
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
