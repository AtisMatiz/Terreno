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

## Criteria — two kinds, deliberately separated

**Adjustable, per run** — `criteria.yaml`, in Portuguese:

```yaml
localizacao:
  estados: [SP]
  regiao: "vale do paraiba"   # null = estado inteiro
  municipios: []              # vazio = região inteira
area:  {min_ha: 2, max_ha: 10}
preco: {min: 100000, max: 2000000}
```

A named `regiao` resolves through `data/regioes.yaml` to a municipality list,
matched accent-insensitively. Ships with Vale do Paraíba, Serra da Mantiqueira,
Sul de Minas, Circuito das Águas and Chapada Diamantina; add a key to add a
region.

Areas are always hectares. Conversion handles the regional units, chosen by the
listing's state: **alqueire paulista 2,42 ha**, **alqueire mineiro/geral 4,84 ha**
(double the paulista, not half), alqueire do norte 2,72 ha, tarefa 0,3025 ha.

**Fixed profile** — `terreno/scoring.py`, in code because it is structural,
versioned and testable. Nine weighted dimensions:

| Dimensão | Peso | O que conta |
|---|--:|---|
| Água | 30 | nascente (mais de uma pontua mais), mina d'água, rio/riacho, cachoeira, lago/represa |
| Benfeitorias | 15 | casa sede, casa de caseiro, mais de uma casa, curral, galpão, energia |
| Sossego | 10 | sossegado, sem vizinhos, reservado — penaliza beira de rodovia e condomínio |
| Acesso | 10 | asfalto, carro comum, km de estrada de terra (≤2 ótimo, >5 penaliza) — "só 4x4" zera |
| Topografia | 10 | plano, pouca declividade, "% plano" quando informado (≥50% pontua cheio) |
| Solo | 10 | terra boa, manejo regenerativo, sem agrotóxico — penaliza degradado, monocultura, eucalipto |
| Mata nativa | 10 | mata nativa, reserva legal, bioma preservado |
| Documentação | 10 | escritura, matrícula, georreferenciamento — penaliza usucapião e posse |
| Distância | 5 | minutos/km até a cidade (≤18 min é o alvo) |

Before scoring, a gate drops anything that does not read as rural property with
a homestead — urban lots, gated developments, town houses. Matching is
accent-insensitive and negation-aware: "sem nascente" never counts as water.
Missing evidence scores zero for that dimension but does not disqualify, since
rural listings are terse and silence is not proof of absence.

## Costs and guards

Every metered resource is capped in `criteria.yaml` and tracked in a SQLite
ledger, so a daily schedule cannot burn a monthly free tier early:

- **Brave** — up to `brave_consultas_por_run`, tapered so the monthly cap lasts
  to the last day of the month. Queries are built round-robin across the
  region's municipalities so a 100-query budget covers the whole region rather
  than the first three towns.
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

The interface is entirely in Portuguese. Sort by pontuação, **mais água**,
recency, preço, R$/hectare or área; filter by estado, município, fonte or free
text; "ver detalhe" opens a per-dimension bar chart showing exactly why a
listing ranked where it did. "novo" and price-drop badges come from the run
history; hiding a listing keeps it hidden (stored in your browser, not the
repo). `site/listings.json` carries the same data for anything else.

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
