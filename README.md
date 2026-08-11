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
| **A′ — leilões** | Comprei PGFN (`comprei.pgfn.gov.br`) | nothing — public API | local only |
| **B — long tail** | Brave Search + page extraction | `BRAVE_API_KEY` (free tier) | anywhere |
| **C — Facebook** | Marketplace and groups | `APIFY_TOKEN`, or burner cookies | local only |

### Sobre os 403 — o que já foi tentado

Medido, não suposto. OLX, VivaReal, Imovelweb, Wimoveis e a API do Mercado
Livre recusam IP de datacenter com 403; o PGFN derruba a conexão por timeout
de rede, não por 403 (então a saída 3 abaixo não ajuda nesse caso). Os
runners do GitHub Actions são datacenter. Quatro saídas, em ordem de
custo-benefício — o CSV da Caixa já foi resolvido pela saída 3 e saiu dessa
lista.

1. **Runner self-hosted** — o mesmo workflow do Actions rodando na sua máquina,
   com o seu IP residencial. Resolve *todos* os 403 de uma vez, é gratuito, e
   mantém a orquestração do GitHub. É a resposta certa se você quiser tudo
   num lugar só: Settings → Actions → Runners → New self-hosted runner, depois
   troque `runs-on: ubuntu-latest` por `runs-on: self-hosted` no workflow.
2. **Busca indexada como ponte** (já ativo) — a Brave é uma API com chave, então
   alcança do CI o conteúdo dos sites que bloqueiam nosso IP. É para isso que
   serve `sites_alvo`: uma consulta `site:wimoveis.com.br` traz os anúncios do
   Wimoveis sem nunca falar com o Wimoveis.
3. **Impressão digital de TLS** — parte desses bloqueios olha o handshake, não o
   IP. `pip install curl_cffi` liga um segundo transporte que imita o Chrome;
   `terreno/http.py` o usa sozinho quando leva 403. **Não foi possível validar
   durante o desenvolvimento**: o sandbox usado passa por um proxy que termina o
   TLS e substitui qualquer impressão digital. Instale e rode uma vez para
   descobrir — o log diz `liberado via curl_cffi` quando funciona.
4. **Proxy residencial pago** — resolve, custa a partir de ~US$ 1/GB, e foi
   descartado por causa do teto de R$ 0.

### Blocked sites — the ones that need a local run

Measured, not assumed, one signature per source: **OLX, VivaReal, Imovelweb and
the Mercado Livre API return HTTP 403** to datacenter IP ranges; **PGFN drops
the connection at the network level** (`ConnectTimeoutError`/`SSLZeroReturnError`,
not a 403 — confirmed on real runs, so a different TLS fingerprint would not
help); **Facebook** is stricter still. GitHub Actions runners are datacenter
IPs, so none of these ever produce a result in CI — only from your own
connection. The canonical list is the `perfis.local` block in `criteria.yaml`
minus whatever also appears in `perfis.ci`; today that's:

- `olx`
- `vivareal`
- `mercadolivre`
- `imovelweb`
- `wimoveis`
- `pgfn`
- `facebook`

`chavesnamao`, `caixa` and `brave` answer fine from GitHub's datacenter IP
(caixa via the `curl_cffi` fallback in `terreno/http.py`) and stay in `ci` too.

```bash
python -m terreno.run --profile ci      # brave + chavesnamao + caixa — what the Action runs
python -m terreno.run --profile local   # everything above, plus ci's sources — your machine
```

Both write to the same SQLite database and the same page, so the result is one
merged view regardless of which half produced a given listing. Edit the
`perfis:` block in `criteria.yaml` to move a source between them — if you get
a Mercado Livre token, for instance, move `mercadolivre` into `ci`.

**Running it manually** — one command, from a clone of this repo on your own
machine (residential IP, not a VPN/VPS):

```bash
cd /path/to/Terreno
./scripts/run_local.sh
```

That script runs `--profile local`, then commits and pushes `data/terreno.sqlite3`
and `site/` if anything changed — same as what the Action does for its half. It
needs a `.env` with whatever keys the blocked sources use (see
[Secrets](#secrets)); `pip install -r requirements.txt` once beforehand if you
haven't. First run prints which sources it's about to hit before fetching
anything, so a bad `.env` or a typo in `criteria.yaml` shows up immediately.

Schedule it with cron so it runs on its own instead of by hand:

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

## Running a search from the page

The page is static, so "rodar busca agora" works by asking a Vercel serverless
function (`api/disparar.js`) to trigger the GitHub Actions workflow. The GitHub
token lives in Vercel's environment and never reaches the browser; the form
sends only the criteria and a shared password.

The function forwards **only** `localizacao`, `area`, `preco` and
`max_preco_por_ha` — a tampered page cannot reach into budgets, sources or
profiles. Criteria arrive at the run as `TERRENO_OVERRIDES` (JSON, deep-merged
over `criteria.yaml`), so a one-off search changes nothing on disk unless
"gravar como padrão" is ticked, which adds `--salvar-criterios`.

Vercel environment variables:

| Variable | Value |
|---|---|
| `GITHUB_TOKEN` | fine-grained PAT, **Actions: read and write**, this repo only |
| `GITHUB_REPO` | `AtisMatiz/Terreno` |
| `GITHUB_REF` | `claude/land-search-scraper-evdukq` |
| `TERRENO_SENHA` | any passphrase; the page asks for it before dispatching |

On GitHub Pages or a local `file://` open the panel still renders but reports
that `/api/disparar` is unreachable — the function only exists on Vercel.

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
