"""Layer B — long-tail discovery via the Brave Search API.

The portals in Layer A cover the big marketplaces. This covers everything else:
regional broker sites, town classifieds, public Facebook posts that Google and
Brave have indexed, and the one-page listings that never reach a portal.

Two guards matter here and are both enforced against the SQLite ledger:
  * a per-run query cap, and
  * a per-month cap, so a daily schedule cannot burn the free tier by the 20th.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timezone

from .. import http
from ..config import llm_enabled
from ..extract import rules
from ..models import Listing

log = logging.getLogger("terreno.sources.brave")

NAME = "brave"
API = "https://api.search.brave.com/res/v1/web/search"
RESOURCE = "brave_queries"

# Query templates. Each is expanded per state (and per municipality when the
# criteria name any), then deduplicated.
TEMPLATES = [
    'terreno à venda {place}',
    'chácara à venda {place}',
    'sítio à venda {place}',
    'fazenda à venda {place} hectares',
    'área rural à venda {place}',
    '"vendo terreno" {place} hectares',
    '"vendo sítio" OR "vendo chácara" {place}',
    'terreno rural {place} site:facebook.com',
    'terreno {place} venda -aluguel -alugar',
]

# Hosts already covered by Layer A — no point spending a page fetch on them.
SKIP_HOSTS = (
    "olx.com.br", "vivareal.com.br", "zapimoveis.com.br",
    "mercadolivre.com.br", "chavesnamao.com.br", "imovelweb.com.br",
)


def _daily_allowance(store, per_run: int, per_month: int) -> int:
    """Spend up to `per_run`, but taper as the month's headroom shrinks so the
    free tier lasts to the last day instead of dying mid-month."""
    now = datetime.now(timezone.utc)
    days_total = monthrange(now.year, now.month)[1]
    days_left = max(1, days_total - now.day + 1)
    remaining = store.budget_remaining(RESOURCE, per_month)
    fair_share = int(remaining // days_left)
    return max(0, min(per_run, max(fair_share, 0)))


def _places(criteria) -> list[str]:
    if criteria.municipalities:
        return [f"{m} {criteria.states[0] if criteria.states else ''}".strip()
                for m in criteria.municipalities]
    from .base import UF_NAMES
    return [UF_NAMES.get(uf, uf).replace("-", " ") for uf in criteria.states]


def fetch(criteria, store, budgets) -> list[Listing]:
    from ..config import env
    token = env("BRAVE_API_KEY")
    if not token:
        log.warning("BRAVE_API_KEY not set — Layer B skipped")
        return []

    allowance = _daily_allowance(
        store,
        int(budgets.get("brave_queries_per_run", 50)),
        int(budgets.get("brave_queries_per_month", 2000)),
    )
    if allowance <= 0:
        log.warning("brave: monthly query budget exhausted — Layer B skipped")
        return []

    queries = [t.format(place=p) for p in _places(criteria) for t in TEMPLATES]
    queries = list(dict.fromkeys(queries))[:allowance]
    log.info("brave: %d queries (allowance %d)", len(queries), allowance)

    already = store.seen_urls()
    candidates: dict[str, str] = {}   # url -> snippet title

    for query in queries:
        data = http.get_json(
            API,
            params={"q": query, "country": "br", "search_lang": "pt",
                    "count": 20, "result_filter": "web"},
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        )
        store.budget_spend(RESOURCE, 1)
        if not data:
            continue
        for result in (data.get("web") or {}).get("results", []):
            url = result.get("url") or ""
            if not url or url in already or url in candidates:
                continue
            if any(host in url for host in SKIP_HOSTS):
                continue
            candidates[url] = result.get("title") or ""

    log.info("brave: %d new candidate pages", len(candidates))
    return _visit(candidates, budgets)


def _visit(candidates: dict[str, str], budgets) -> list[Listing]:
    """Fetch each candidate once and extract. Rules first; the model only sees
    pages the rules could not read, and only when it is switched on."""
    cap = int(budgets.get("max_new_pages_fetched", 200))
    use_llm = llm_enabled()
    out: list[Listing] = []

    for url, hint in list(candidates.items())[:cap]:
        resp = http.get(url, timeout=20, retries=2)
        if resp is None or "text/html" not in resp.headers.get("content-type", ""):
            continue

        listing = rules.extract(resp.text, url, source=NAME)
        if use_llm and rules.is_thin(listing):
            from ..extract import llm
            better = llm.extract(resp.text, url, source=NAME)
            if better:
                listing = better

        if listing:
            listing.title = listing.title or hint
            out.append(listing)

    log.info("brave: %d listings extracted", len(out))
    return out
