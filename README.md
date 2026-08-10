# Terreno

Autonomous search for rural land for sale in Brazil. Collects from marketplace
portals, general web search, and Facebook; filters and scores against criteria
you set in one YAML file; publishes a single static page you can sort visually
and click straight through to the offer.

```
criteria.yaml ─► sources ─► normalize ─► dedup ─► filter ─► enrich ─► score ─► SQLite ─► site/index.html
                                                                                   └─► Telegram (new only)
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in what you have; every key is optional
$EDITOR criteria.yaml         # where, how much, what qualities matter
python -m terreno.run --dry-run          # print matches, write nothing
python -m terreno.run --profile local    # real run, writes DB + page
open site/index.html
```

Nothing is required to get a first result: with no keys at all, the Chaves na
Mão source still runs and the page still renders.

## The three layers

| Layer | Sources | Needs | Where it runs |
|---|---|---|---|
| **A — portals** | OLX, VivaReal/ZAP, Mercado Livre, Chaves na Mão, Imovelweb | nothing (ML wants a free token) | mostly local, see below |
| **B — long tail** | Brave Search + page extraction | `BRAVE_API_KEY` (free tier) | anywhere |
| **C — Facebook** | Marketplace and groups | `APIFY_TOKEN`, or burner cookies | local only |

### Why there are two profiles

Measured, not assumed: **OLX, VivaReal, Imovelweb and the Mercado Livre API all
return HTTP 403 to datacenter IP ranges**, and GitHub Actions runners are
datacenter IPs. Facebook is stricter still. So the work is split:

```bash
python -m terreno.run --profile ci      # brave + chavesnamao — what the Action runs
python -m terreno.run --profile local   # everything else — your machine
```

Both write to the same SQLite database and the same page, so the result is one
merged view regardless of which half produced a given listing. Edit the
`profiles:` block in `criteria.yaml` to move a source between them — if you get
a Mercado Livre token, for instance, move `mercadolivre` into `ci`.

Schedule the local half with cron:

```
0 7 * * * cd /path/to/Terreno && ./scripts/run_local.sh >> /tmp/terreno.log 2>&1
```

## Criteria

Everything lives in `criteria.yaml` — states, price and area bounds, an
optional R$/hectare ceiling, an optional radius around a reference point, and
three lists of free-text qualities:

```yaml
must_have:     [agua, acesso, matricula]
nice_to_have:  [mata, energia, benfeitorias]
deal_breakers: [posse, invasao, litigio]
```

Those names map to Portuguese synonym sets in `terreno/qualities.py` — `agua`
matches *córrego*, *nascente*, *açude*, *poço artesiano* and so on — and
matching is accent-insensitive and negation-aware, so "sem água" does not count
as a hit. A deal-breaker disqualifies a listing outright; missing must-haves
cost score but do not disqualify, because rural listings are terse and silence
is weak evidence.

Areas are converted to hectares including the regional units: **alqueire
paulista 2.42 ha, alqueire mineiro/geral 4.84 ha, tarefa 0.3025 ha**, chosen by
the listing's state.

## Costs and guards

Every metered resource is capped in `criteria.yaml` and tracked in a SQLite
ledger, so a daily schedule cannot burn a monthly free tier early:

- **Brave** — up to `brave_queries_per_run`, tapered so the monthly cap lasts to
  the last day of the month.
- **Apify** — checked against both the local ledger and Apify's own reported
  limits before each run.
- **Page fetches** — listings are only fetched in full *after* they pass the
  hard filters (`pipeline.enrich`). Chaves na Mão encodes price and area in the
  URL slug, so thousands of candidates cost zero requests until a few dozen
  survive filtering.

The LLM is **off by default**. With `ENABLE_LLM=1` and an `ANTHROPIC_API_KEY`
it is consulted only for pages the free rules-based extractor could not read.

## The page

`site/index.html` is self-contained — data embedded as JSON, sorting and
filtering client-side. It works from GitHub Pages, from Vercel (`vercel.json`
points at `site/`), or opened directly off disk.

Sort by fit, recency, price, R$/hectare, or area; filter by state, source, or
free text; "novo" and price-drop badges come from the run history; hide a
listing and it stays hidden (stored in your browser, not the repo).
`site/listings.json` carries the same data for anything else you want to build.

## Secrets

Local runs read `.env`. CI reads repository secrets — Settings → Secrets and
variables → Actions:

| Name | Unlocks |
|---|---|
| `BRAVE_API_KEY` | Layer B |
| `APIFY_TOKEN` | Facebook via Apify |
| `ML_ACCESS_TOKEN` | Mercado Livre |
| `ANTHROPIC_API_KEY` + variable `ENABLE_LLM=1` | LLM extraction and scoring |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | new-match pings |
| variable `TERRENO_PAGE_URL` | link inside the Telegram message |

`FB_COOKIES_FILE` is local-only and must never become a repository secret.

> Scraping Facebook is against Meta's terms of service. Use a throwaway
> account; the account whose cookies you supply is the one at risk.

## Adding a source

Write `fetch(criteria, store, budgets) -> list[Listing]`, add one line to
`terreno/sources/__init__.py`, and add the name to `sources:` in the YAML. Fail
soft — return `[]` on trouble; `run.py` isolates each source so a broken portal
costs only its own listings.
