# Terreno — Session Log

Living document. Append new sessions at the bottom under "## Session history". Keep the "Standing rules" and "Architecture / current state" sections up to date — they're what future sessions need to pick up work without re-litigating decisions.

> **Note on secrets**: this file intentionally contains **no raw API keys, passwords, or private keys**. Anything generated in a session (tokens, scratchpad files) lives in that session's ephemeral scratchpad and is **gone once the session ends**. Treat credential retrieval as "go get it from the dashboard again" in every new session, not "read it from disk."

---

## Standing rules (apply to every future session on this project)

- Ask a question once, then wait — no re-asking, no restating answers already given. (Also in CLAUDE.md, both global and project-level.)
- Claude can and should operate GitHub Actions directly (trigger runs, poll status, read logs via the `mcp__github__actions_*` / `get_job_logs` tools) instead of asking the user to download/paste logs.
- Repo `AtisMatiz/Terreno` is **public**, so GitHub Actions minutes are free/unmetered — job timeouts can be raised generously without a real cost tradeoff; the only reason to keep them bounded is as a backstop against a genuine hang.
- PGFN (`comprei.pgfn.gov.br`) is back in `perfis.ci` (2026-08-12) — curl_cffi clears it from a datacenter IP, measured on a real Actions run. Same for `olx` (via the safari/firefox rung of `IMPERSONATE_ESCADA`) and `wimoveis` (via ZenRows). `imovelweb` stays local-only: refused by everything measured, including the paid unblocker.
- **A source returning zero results is not evidence of a block.** Established the hard way, three separate times: VivaReal's zero was our own oversized `size` param, Mercado Livre's was a missing OAuth token, Facebook's was a payload that matched no field in the actor's schema. Read the response body before concluding anything about the network.
- **Measure in the right place.** This sandbox routes through a TLS-terminating proxy, so any fingerprint measurement taken here is meaningless — but GitHub Actions runners do **not** intercept TLS, they merely have a datacenter IP. Those are different questions and conflating them cost real time. `scripts/diagnostico.py` + the `diagnostico` workflow exist to answer them where the answer means something.
- When polling a long-running GitHub Actions run, use a real external timer (`Monitor` with `curl`, or a `Bash --run_in_background` loop hitting the GitHub API) — never judge run duration from this sandbox's own elapsed time / background-sleep completions, which do not reliably track real wall-clock time here. Misreading elapsed time as "stuck" previously caused a live, healthy run to be cancelled by mistake.
- Front-load every access request for the session's likely work at the start (check `SuggestConnectors`/`ListConnectors` before asking for a raw key) rather than piecemeal mid-task. Not standing authorization for destructive/production-facing/hard-to-reverse actions — those still get a check-in at the moment they're taken, regardless of what's been granted upfront.
- Delegate cheap, judgment-free subtasks (file/codebase search, fetching/summarizing docs, running a known script and reporting results, repetitive checks across files) to Haiku subagents (Agent tool, `model: "haiku"`) instead of doing them inline. Reserve the default model for real judgment calls. **This was violated on 2026-08-12**: five parallel agents were spawned without `model:` set and silently inherited Opus (~370k subagent tokens for work that was substantially mechanical). The `Agent` call's default is the parent's model, not Haiku — the model kwarg must be passed explicitly every time, it is never assumed.
- When genuinely stuck (same fix retried repeatedly, diminishing returns, a hard blocker — not just "this is slow"): generate a genuinely different strategy, try it in an isolated git worktree (`Agent` with `isolation: "worktree"`), verify it actually works before trusting it, then merge if better or discard cleanly if not.
- Chat output: no play-by-play narration of intermediate tool calls. Final message is a succinct result, a summary of what changed, and what's needed from the user if anything.
- **Merge PRs without asking** (user instruction, 2026-08-11: "merge, don't ask, just do it"). Open the PR, merge it, keep going — don't pause for approval on the merge step. Standing authorization for merges specifically, not a blanket waiver on genuinely destructive/irreversible actions.
- The user is **not a programmer**. Terminal instructions need to be literal, one command at a time, with what-you'll-see spelled out — and watch for interactive traps a developer would breeze past (vim opening on `git pull`, hidden password prompts, commands accidentally pasted onto one line). Set `git config --global core.editor "nano"` on their machine if vim comes up again.

---

## Architecture / current state (as of 2026-08-11)

**What it does**: searches Brazilian portals + long-tail web/social for rural land for sale, filters/scores against `criteria.yaml`, publishes a sortable HTML page via GitHub Pages (`https://atismatiz.github.io/Terreno/`).

**Pipeline** (`terreno/run.py`): iterates `REGISTRY` sources (`terreno/sources/__init__.py`) enabled for the active profile (`ci` or `local`, in `criteria.yaml`), collects raw listings, normalizes/dedupes/filters/enriches/scores (`terreno/pipeline.py`), renders `site/index.html` (`terreno/render.py`), commits `data/terreno.sqlite3` + `site/` + `criteria.yaml` back to the branch, then a `deploy` job publishes to Pages.

**Sources**: `mercadolivre`, `pgfn`, `olx`, `chavesnamao`/`imovelweb`/`wimoveis` (all via `htmlportal.py`), `caixa` (public CSV + `curl_cffi` for its bot wall), `brave` (long-tail via Brave Search API), `facebook`. `vivareal` is **disabled** (`fontes.vivareal: false`, 2026-08-11) — the portal misbehaves in an ordinary browser too, so it was not worth fixing. Profiles (updated 2026-08-12 from the diagnostic below): `ci` = `brave, chavesnamao, caixa, pgfn, olx, wimoveis`; `local` = everything (kept as the fallback/manual path via `scripts/run_local.sh`).

**Measured reality of each source** — one table for the residential run (2026-08-11) and one for the datacenter/CI run (2026-08-12); the two environments need different transports for the same host, which is why both are kept:

| Source | From the user's Mac (2026-08-11) | Cause |
|---|---|---|
| `caixa` | ✅ 200 | works |
| `chavesnamao` | ✅ 43 listings | works |
| `wimoveis` | ✅ 46 listings directly, no unblocker needed | works residentially; a datacenter IP needs ZenRows for the same host, see below |
| `olx`, `imovelweb` | ❌ 403 | genuine block from this IP too |
| `pgfn` | ❌ `SSLZeroReturnError` | refused at the TLS handshake |

**From a GitHub Actions runner (2026-08-12, `scripts/diagnostico.py`, run 31630009812)** — settles which transport each host actually needs from CI:

| Source | requests | curl_cffi | ZenRows | Verdict |
|---|---|---|---|---|
| `caixa` | ✅ 200 | — | — | works as-is |
| `pgfn` | SSLError | ✅ 200 (chrome) | not needed | curl_cffi alone; never reached before this session's reachability fix |
| `olx` | 403 | ✅ 200 (**safari/firefox only**, chrome refused) | 422 | curl_cffi via `IMPERSONATE_ESCADA` |
| `wimoveis` | 403 | 403 (all 4 browsers) | ✅ 200 | needs the paid unblocker from a datacenter IP |
| `imovelweb` | 403 | 403 (all 4 browsers) | 422 | **nothing clears it** — stays local-only |
| `mercadolivre-api` | 403 | 403 | 422 | unrelated to this table — needs OAuth, see below |
| `mercadolivre` | ❌ 403 | **not a block** — API needs OAuth; now uses `client_credentials`, awaiting the user's app credentials |
| `facebook` | ❌ 400 | **was our bug** — wrong Apify payload, fixed |

**Scoring** (`terreno/scoring.py`, 2026-08-11 rework): seven dimensions summing to 100 — Água 30, Benfeitorias 20, Acessibilidade 15, Silêncio 10, Aptidão agroflorestal e solo 10, Documentação 10, Topografia 5. Plus two modifiers outside the base, each exposed as its own row in `dimensoes`: **preço/ha** (bonus below `preco_por_ha.ideal`, penalty above `teto_alerta`) and **proximidade** to `localizacao.centro` (Monteiro Lobato). `motivo_descarte()` handles hard discards — dirt road >8 km, `contrato de gaveta`, non-rural, area <0.5 ha — kept separate from scoring so the caller decides. `estrelas()` surfaces cachoeira and similar without scoring them; `destaques()` builds the per-theme strings the Telegram card prints.

**Two output channels, deliberately different widths.** Everything scored reaches the site. Telegram gets a narrower list, gated in `run.py` by two rules that both *leave the listing on the site*: price per hectare above `teto_alerta` (`notificavel`), and already sold/removed (`terreno/disponibilidade.py`, checked in parallel over the shortlist only, where a network failure counts as available so an outage cannot delete a good listing).

**Photo reading** (`terreno/extract/imagem.py`): Haiku vision, gated to scores ≥ `saida.nota_minima_imagem` (70/100). Keywords are free and run on everything; images cost per listing, so the spend tracks the shortlist. Needs `ENABLE_LLM=1` + `ANTHROPIC_API_KEY`, otherwise it logs once and skips.

**Unblocker transport** (`terreno/http.py`): escalation is `requests` → `curl_cffi` → ZenRows. Off unless `TERRENO_UNBLOCKER=zenrows` and `ZENROWS_API_KEY` are both set (repository secret + variable, added by the user 2026-08-11). Hard per-process cap `TERRENO_UNBLOCKER_MAX_POR_RUN` (default 15 requests) because `brave_visit` walks the whole pending queue and could otherwise spend a month's free allowance in one run. Key is redacted from every log line.

**The user's local setup is working** (macOS, Python 3.13 at `/Library/Frameworks/Python.framework/Versions/3.13`): repo cloned at `~/Documents/Terreno`, `.env` filled in, `./scripts/run_local.sh` runs end-to-end, pushes results, and Telegram alerts arrive. Their `pip3` and `python3` point at **different Python installs** — always tell them `python3 -m pip install ...`, never bare `pip3`.

**Brave (Layer B)** is two decoupled phases, each its own module:
- `terreno/sources/brave_discover.py` — queries the Brave API, queues every new candidate URL into the `brave_pendentes` SQLite table. Never fetches a page. `brave_consultas_por_run` caps queries per run (600, raised 2026-08-11); `brave_consultas_por_mes` is a leftover from the old free tier's 2000/month cap and is now set to a placeholder-high number since the current plan (confirmed on Brave's dashboard) has no monthly cap, only 50 req/s. Also builds `site:` queries for both the hand-curated `sites_alvo` list and any auto-discovered hosts due for their weekly check (see below).
- `terreno/sources/brave_visit.py` — loads the *entire* pending queue and visits it in parallel (`ThreadPoolExecutor`, `brave_paralelismo: 50`), no per-run time or count cap. Per-page timeout `brave_timeout_pagina_s: 40`. Three outcomes per candidate: extracted a listing → done; fetched but nothing there → done (discarded, revisiting won't help); couldn't fetch at all → `falhas` counter +1 in the queue, retried next run, discarded after `brave_max_falhas: 2` consecutive failures. Also feeds `Store.registrar_extracao_brave()` on every successful extraction, which is how new specialized sites get discovered (below).
- `terreno/sources/brave.py` is a thin orchestrator: `discover()` then `visit_all()`, same external `fetch()` signature the registry expects.
- The old shared 90s visit deadline is gone entirely — visiting was never actually API-limited (only discovery calls Brave), so the real backstop is the workflow job's own `timeout-minutes` (currently 90, in `.github/workflows/search.yml`), not an app-level budget.

**Auto-discovered sites** (added 2026-08-11): a host that isn't in `sites_alvo` but produces ≥2 real extracted listings (price/area actually read, not a category page) gets "promoted" in the new `sites_descobertos` table and joins the `site:` query rotation — on its own weekly clock (`ultima_consulta`), independent of how often the main pipeline runs. Logic: `Store.registrar_extracao_brave`/`sites_descobertos_para_consultar`/`sites_descobertos_marcar_consultado` in `terreno/store.py`, wired from `brave_visit.py` (recording) and `brave_discover.py` (querying + resetting the clock).

**Database** (`terreno/store.py`, `data/terreno.sqlite3`, committed to the repo): `listings`, `price_history`, `geocache`, `budget_ledger`, `runs`, `source_health`, `brave_pendentes` (url, dica, descoberto_em, falhas), `sites_descobertos` (host, ocorrencias, primeira_vez, ultima_vez, promovido_em, ultima_consulta). **Important**: `Store.__init__` runs `SCHEMA` via `CREATE TABLE IF NOT EXISTS` *and* an explicit `_migrate()` step — the schema-diff pattern needed any time a column is added to an existing table, since `IF NOT EXISTS` never alters a table that already exists and this DB is a committed file, not recreated per run. (A brand-new table like `sites_descobertos` doesn't need this — `CREATE TABLE IF NOT EXISTS` handles that fine on an old DB file.)

**Infra**: GitHub Actions (`.github/workflows/search.yml`) — cron daily at 09:00 UTC + manual `workflow_dispatch` (accepts `profile`, `overrides`, `salvar` inputs). GitHub Pages serves the static site. **Vercel** (`api/disparar.js`) exists only to hold the GitHub PAT server-side for a "run search now" button on the page — GitHub Pages can't hold secrets since it's static-only. User has Vercel "already configured" on their end but branch/env-var wiring for the button isn't confirmed done yet (see Pending).

**Search criteria** are still narrow test values: SP, Vale do Paraíba, 2–10 ha, R$100k–2M — the simplest lever to get more results is widening these.

---

## Known bugs & fixes (reference for anyone touching this again)

- **Brave HTTP 422**: two separate causes, both fixed. (1) `country` param must be uppercase (`"BR"`, not `"br"`). (2) `search_lang: "pt"` fails Brave's enum validation (wants a full locale like `pt-BR`) — dropped entirely since the query text is already Portuguese.
- **curl_cffi never actually exercised against Caixa**: `http.py`'s automatic curl_cffi fallback only triggers on HTTP 403/429, but Caixa's Radware bot wall returns HTTP 200 with a CAPTCHA page — so `caixa.py` calls `http._via_cffi` directly in its own retry chain instead of relying on the generic fallback. Also `curl_cffi` was commented out in `requirements.txt` until promoted to a real dependency — check it's actually installed if this regresses.
- **PGFN (`comprei.pgfn.gov.br`) fails at the TLS handshake (`SSLZeroReturnError`), from CI *and* from a residential connection** (the latter measured 2026-08-11). ⚠️ The earlier note here claimed this proved `curl_cffi` "does not help" — that conclusion was unfounded and has been retracted: `http.py` only ever invoked `curl_cffi` from the `403/429` status-code branch, so an exception-shaped failure like this one never reached it. curl_cffi had literally never been tried against PGFN. Fixed (see below); now genuinely testable.
- **Three compounding bugs in `terreno/http.py`'s curl_cffi fallback** (all fixed 2026-08-11) — together they made the fallback look useless while barely functioning:
  1. **Never reached on exception-shaped failures.** Only the `403/429` branch called it, so TLS-level rejections and connection resets (PGFN's exact failure) silently skipped it. Now attempted on `RequestException` too.
  2. **Its own headers defeated it.** `_via_cffi` passed `{**DEFAULT_HEADERS, **headers}`, overriding the User-Agent/Accept that `impersonate` carefully matches to the TLS+HTTP/2 handshake. Cloudflare/DataDome cross-check exactly that pairing, so a "Chrome 124 on Windows" UA over a current-Chrome handshake is *itself* a bot signal — the fallback was sabotaging its own disguise. Now only semantically-meaningful headers (`x-domain`, `Origin`, `Referer`, API tokens) are forwarded; see `_HEADERS_DE_IMPRESSAO`.
  3. **Its failures were invisible.** The only failure log was at DEBUG, and the host was marked "already tried" *before* the attempt, so a normal run could not distinguish "curl_cffi was tried and blocked" from "curl_cffi isn't installed" — two states needing opposite responses. Both now log at WARNING with the actual status code, and the impersonation target is settable via `TERRENO_IMPERSONATE` for one-command experiments.
- **VivaReal's "0 listings" was never a block at all** — `PAGE_SIZE = 100` exceeded the glue-api's cap, which answers `HTTP 400 {"message": "Size is above acceptable limit"}`. It read as a block only because from a datacenter IP Cloudflare returns 403 *before* the API can report the real error, so nobody ever saw the 400. Lowered to 24. (Source has since been disabled at the user's request — the portal misbehaves in a normal browser too — but the parsing lesson stands: **a source reporting zero is not evidence of a block**.)
- **"curl_cffi ausente" when it is actually installed** — `pip3` and `python3` were different Python installations on the user's Mac, so pip reported `Requirement already satisfied` while the script could not import it. Compounding it, both `http.py` and the diagnostic caught only `ImportError` and reported "not installed" — but curl_cffi ships a compiled extension whose failed load raises `OSError`, so a *broken* install was indistinguishable from a *missing* one. Both now catch broadly and print the real exception plus `sys.executable`. **Always instruct `python3 -m pip install`**, which installs into the running interpreter by construction.
- **Apify actor input schema must be read, not guessed** — every Facebook call failed with `400 Field input.startUrls is required` because the code sent `search`/`maxItems`/`country`, none of which exist in that actor's schema. The schema is public and free to read: `GET https://api.apify.com/v2/acts/<actor>/builds/default` → `data.inputSchema`. Do that before changing a payload again.
- **`.env` was only ever loaded by `scripts/run_local.sh`, never by the Python itself** (fixed 2026-08-11). `config.env()` was a bare `os.getenv`, so `python -m terreno.run ...` — the exact command the README's Quick start gives you, immediately after telling you to create a `.env` — silently ignored the whole file and every source reported its credential as missing while it sat on disk. Cost real debugging time: the user's ML credentials looked broken when they were fine. `config._carregar_dotenv()` now loads it at import for any entry point, with `setdefault` so genuine environment variables (CI secrets) still win over the file.
- **Mercado Livre's 403 is an auth failure, not an IP block** — their API requires OAuth. The 403 is the documented response to an anonymous caller, and a residential connection gets the identical one, so no amount of fingerprinting or IP change will ever fix it.
- **Mercado Livre access tokens expire in ~6 hours**, so the original `ML_ACCESS_TOKEN`-pasted-into-`.env` design could only ever have worked for one afternoon before silently taking the source down. Replaced (2026-08-11) with `ML_CLIENT_ID`/`ML_CLIENT_SECRET` and the OAuth **`client_credentials`** grant, which authenticates the *application* — no browser redirect, no user step, nothing to renew by hand. `ML_ACCESS_TOKEN` still works as a manual override. Verified: `POST https://api.mercadolibre.com/oauth/token` is reachable **from a datacenter IP** and returns a precise error body (`invalid_client` for bad credentials), so minting works from CI too — whether `/sites/MLB/search` then accepts the token is the remaining unknown.
- **Stored XSS in `terreno/templates/page.html`**: every scraped field (including public Facebook search results — untrusted third-party content) was inserted via `innerHTML` with no escaping. Fixed with `escHtml()` (text) and `safeUrl()` (href/src, only allows http/https else collapses to `#`). Verified against a real malicious payload run through the actual pipeline.
- **Telegram notify HTML-escaping**: listing titles/URLs interpolated raw into a Telegram HTML-parse-mode message could contain a stray `<`/`&` and silently kill the whole notification. Fixed with a `_esc()` helper in `notify.py`.
- **Health-tracking must stay after `--dry-run`'s early return** in `run.py` — a preview run must never write to `source_health`, or it corrupts the real alert streak.
- **Git commit message + backticks inside double-quoted `-m "..."` in bash**: shell command substitution silently eats the backticked text. Cosmetic-only when it happened once; use single-quoted heredocs for commit messages with backticks/code references.
- **This sandbox's background-sleep timers don't reliably track real wall-clock time** — caused a real false "the run is deadlocked" alarm and a live healthy run got cancelled by mistake. Always poll external state (GitHub API) directly via `Monitor`/`curl`, never infer stuck-ness from elapsed sandbox time.
- **Brave-sourced listings pointing at a generic category/search page, not the actual offer** (fixed 2026-08-11): `terreno/extract/rules.py`'s regex fallback grabbed the *first* "R$ ..." and area mention on the page, which on an index/listing page (e.g. "Chácaras e sítios à venda Guaratinguetá - SP") is some unrelated card's price, not a listing about that URL at all. Fixed with two guards in `extract()`: (1) more than one qualifying JSON-LD node (`Product`/`Offer`/`RealEstateListing`/etc.) on the page → bail, no honest way to say which node the URL is "about"; (2) ≥3 distinct "R$ ..." mentions in the page text when the price wasn't read from structured data → bail, almost certainly a page listing several properties. Structured (JSON-LD) data is still trusted even amid other prices on the page (e.g. a "similar listings" sidebar).

---

## Pending / next steps

1. **⏳ IN PROGRESS: confirm pgfn/olx/wimoveis actually produce listings in a real CI search run, not just 200s in the diagnostic.** `perfis.ci` was updated 2026-08-12 and `search.yml` now passes `ZENROWS_API_KEY`/`TERRENO_UNBLOCKER` (it never had before — would have silently failed wimoveis even with the profile fixed). Trigger `search.yml` and read the run log for these three sources' listing counts.
2. **⏳ ZenRows RESP001 fix shipped, unverified live (2026-08-12).** Researched: `RESP001` = "Could not get content" — ZenRows' own docs recommend `js_render=true` as the next step for this specific code, and it is not a free-plan gate (premium_proxy has no plan restriction in their docs; it's paid per credit on every tier). `http.py` now retries once with `js_render=true` only when the first attempt returns exactly `422 RESP001` — costs ~25 credits instead of ~10, so it's gated tightly rather than applied to every 422. Verified against a stubbed transport only; needs a real ZenRows call against olx/imovelweb to confirm it actually clears them (mercadolivre-api's 422 is likely moot regardless, since that source uses its own OAuth path, not the unblocker, once ML_CLIENT_ID/SECRET land).
3. **⏳ Facebook/Apify payload fixed but never exercised against a live call** — schema matches the actor's declared input, but nobody has confirmed the Marketplace URL forms return anything, or that `_from_apify()`'s field-name guesses (`listingUrl`, `marketplace_listing_title`, `listing_price.formatted_amount`, etc.) match the actor's real output shape. Needs the user's `APIFY_TOKEN` status — ask directly rather than assuming absent.
4. **The user wants a button, not a terminal** — stated explicitly, still not delivered. After item 1, the local run may matter much less (three of the five previously-local-only sources now run in CI unattended) — **check with the user whether the double-click Mac app is still wanted** before building it, rather than assuming yes.
5. **Widen search criteria** in `criteria.yaml` — still narrow test values (SP/Vale do Paraíba, 2–10 ha, R$100k–2M). Now that pgfn/olx/wimoveis run in CI, this is the highest-leverage lever left on result count.
6. **Verify `sites_descobertos` end-to-end on a real Brave run** — promotion at the 2-occurrence threshold and the weekly `ultima_consulta` gate are unit-tested against `Store` directly, but no real host has been observed promoting through `brave_visit.py` → `brave_discover.py` yet.
7. **Watch `brave_pendentes` failure counts over the next few runs** — retry-then-discard (`falhas`, threshold 2) was seen queueing 171 retries but a full discard cycle has never been observed live.
8. **⏳ Mercado Livre: user is mid-way through creating the app** at developers.mercadolivre.com.br. Code is done and waiting on `ML_CLIENT_ID` + `ML_CLIENT_SECRET`. Once it works, move `mercadolivre` from `perfis.local` into `perfis.ci`. **Unverified**: whether `/sites/MLB/search` accepts an app token at all — ML has been tightening that endpoint (its own ZenRows probe also 422'd), and if it refuses, Brave's `site:mercadolivre.com.br` coverage is the fallback.
9. The "delegate cheap subtasks to Haiku subagents + automated fix/test/merge loop" architecture idea — discussed, assessed, **not built**. Still open.

---

## Session history

### 2026-08-12 (MEASURED: what is actually blocked, and what never was)
**The decisive run.** `diagnostico` on a GitHub Actions runner (run 31630009812), with ZenRows enabled:

| fonte | requests | curl_cffi (chrome) | curl_cffi (outros) | ZenRows |
|---|---|---|---|---|
| `pgfn` | SSLError | **OK 200** | — | não precisou |
| `olx` | 403 | 403 | **OK 200 com safari e firefox** | 422 |
| `wimoveis` | 403 | 403 | 403 | **OK 200** |
| `imovelweb` | 403 | 403 | 403 | 422 |
| `caixa` | OK 200 | OK 200 | — | não precisou |

- **PGFN is fixed and free.** curl_cffi clears it from a datacenter IP. The long-standing note that "curl_cffi does not help PGFN" was never a measurement — curl_cffi was only ever invoked from the 403 branch, and PGFN fails as an exception, so it had never once been tried. Fixing that reachability bug was the whole fix.
- **OLX is fixed and free, but only under `safari`/`firefox`.** `chrome` and `chrome124` are both refused. A single fixed imitation target therefore reports "blocked" for a host that is not blocked — the choice of imitation *is* the result. Hence `IMPERSONATE_ESCADA`: walk the rungs, remember the winner per host, re-walk only when the answer is unknown.
- **wimoveis needs the paid unblocker** (ZenRows clears it; nothing free does).
- **imovelweb is refused by everything**, including ZenRows (422). Brave's `site:` coverage is what is left for it.
- Bug caught in my own ladder before it shipped: the `_cffi_ok` fast path re-used the *default* imitation instead of the memorised winner, so a host cleared by `safari` would have paid a guaranteed 403 on every request before rediscovering safari. Fixed and covered by a test.


### 2026-08-11 (Mercado Livre OAuth via client_credentials)
- User started creating the ML app; walked them through the form (only the `Client Credentials` flow and the `Mercado Livre` business unit matter — every permission can stay "Sem acesso", no notification topics, redirect URI is required by the form but unused by this grant).
- Found the design flaw before it bit: the existing `ML_ACCESS_TOKEN`-in-`.env` approach could never have worked for more than one afternoon, because ML tokens expire in ~6 hours. Rewrote the source to mint its own token per run via the `client_credentials` grant (authenticates the application, so no browser step and nothing to renew), keeping `ML_ACCESS_TOKEN` as a manual override. Added `ML_CLIENT_ID`/`ML_CLIENT_SECRET` to `.env.example` and the workflow's env block.
- Verified the token endpoint is reachable from a datacenter IP and returns a precise error body, so this will work from CI as well — exercised the real POST path with deliberately wrong credentials and got `invalid_client` back.
- Also made `http.py` log the response body on 403/429. It previously discarded it, so "token inválido", "scope insuficiente" and "your IP is blocked" were indistinguishable — the exact ambiguity that had ML's auth failure misfiled as an IP block. The generic-4xx branch had logged bodies all along for precisely this reason; the 403 branch just never got the same treatment.

### 2026-08-11 (first real local run — onboarding + curl_cffi load diagnosis)
- Walked the user (non-programmer) from a bare Mac to a working local run: Xcode CLT via `xcode-select --install` (the dialog does not appear on its own), Python 3.13, clone, `.env`, `run_local.sh`, and a GitHub PAT for the push. Also rescued them from vim opening on `git pull` (`Esc`, `:wq`) and set `core.editor` to nano.
- The run worked: 233 raw listings, Telegram alerts delivered, results pushed. That also produced the first honest per-source measurement from a residential IP — the table now in Architecture.
- Their `scripts/diagnostico.py` run reported `curl_cffi ausente` despite pip saying it was installed: `pip3` and `python3` are different Python installs on that machine. Fixed the reporting rather than just telling them the workaround — both `http.py` and the diagnostic caught only `ImportError` and so reported a *broken* compiled-extension load as a *missing* package; they now catch broadly and print the real exception plus `sys.executable` (PR #5).
- Net: the 403 question is still open, but it is now blocked on one specific measurement rather than on speculation, and the tooling to take that measurement reports its own failures honestly.

### 2026-08-11 (Facebook/Apify payload rebuilt from the real schema)
- Merged PR #3. Fixed the Apify 400 by reading the actor's declared input schema from Apify's public API instead of guessing: it requires `startUrls` (array of `{"url": ...}`) with optional `resultsLimit`/`includeListingDetails`, and the code was sending `search`/`maxItems`/`country` — three fields that do not exist in that schema, which is why *every* call had failed.
- Collapsed the per-state loop into one actor call (the actor takes the whole URL list itself, so per-state calls multiplied cost for no extra coverage) and turned `includeListingDetails` on by default: scoring reads water/area/buildings out of the listing text, and a Marketplace card title alone rarely carries a hectare figure, so without details most results would be filtered out and the credit wasted on them.
- Caught two defects in my own first version while testing it: the generic-search URL (the only one not depending on a guessed city slug) was appended *last* and so got truncated away by the URL cap, and the alphabetical cut silently pinned coverage to the first few municipalities forever. Generic URL now comes first and is never cut; truncation now logs which municipalities it covered and how to raise the cap.

### 2026-08-11 (403 investigation — three http.py bugs found)
- Merged PR #2, then investigated why OLX/ML/VivaReal 403'd and PGFN failed even from the user's residential connection.
- **The premise was wrong**: "datacenter IP" had been treated as one cause behind one symptom, but the user's own run log contained four *different* failures filed under it. Separating them was most of the work: ML = missing OAuth token (documented, already warned about in its own log line); VivaReal = our own oversized `size` parameter, visible as an HTTP 400 only from residential because Cloudflare 403s first from a datacenter IP; PGFN = TLS-handshake rejection; OLX/imovelweb = genuine 403s.
- Found three compounding bugs in `terreno/http.py`'s curl_cffi fallback (never reached on exception-shaped failures; its own headers defeating the impersonation; failures invisible at INFO) — all fixed, all detailed in Known bugs. The second is the interesting one: the fallback was passing `DEFAULT_HEADERS` over curl_cffi's browser impersonation, i.e. announcing a different browser in the headers than in the TLS handshake, which is a stronger bot signal than plain `requests` would have been.
- Retracted the prior note claiming PGFN proved curl_cffi useless — curl_cffi had never actually run against PGFN, since only the 403 branch invoked it.
- Added `scripts/diagnostico.py`: isolates fingerprinting from IP blocking per source, because **this sandbox physically cannot measure the difference** (TLS-terminating proxy). Verified the new http.py logic against stubbed transports instead (exception path now reaches curl_cffi; semantic headers forwarded while UA/Accept are not; both failure modes now distinguishable in the log).
- Disabled `vivareal` at the user's request (portal misbehaves in a normal browser too), keeping the `PAGE_SIZE` fix and the lesson: a source reporting zero results is not evidence of a block.

### 2026-08-11 (generic-URL fix + Brave scale-up + auto-discovered sites)
- Guided the user (non-programmer, Mac) through their first local run end-to-end from a bare machine: Xcode Command Line Tools, Python install, `git clone`, `.env` setup, `./scripts/run_local.sh`, and a GitHub Personal Access Token for the push (git password auth is retired). Confirmed working: Telegram received real match notifications from the run.
- User reported Telegram links pointing at generic category pages instead of specific listings; confirmed the Brave plan has no monthly cap (50 req/s only, screenshot from the Brave dashboard); asked for new specialized sites to be auto-discovered and re-scraped weekly.
- Fixed the generic-page bug in `terreno/extract/rules.py::extract()` — see Known bugs above.
- Raised `brave_consultas_por_run` 100→600 and `brave_consultas_por_mes` to a placeholder-high number in `criteria.yaml`, since the plan has no real monthly cap.
- Built the auto-discovered-sites mechanism: new `sites_descobertos` table + `Store` methods (`registrar_extracao_brave`, `sites_descobertos_para_consultar`, `sites_descobertos_marcar_consultado`); `brave_visit.py` records a successful extraction's host (excluding already-scraped/curated hosts and Facebook) and promotes it after 2 occurrences; `brave_discover.py` folds promoted, due-for-the-week hosts into its `site:` query rotation and resets their clock only for the ones actually queried this run (allowance truncation can crowd some out).
- Since the branch (`claude/init-gagwem`) had already been merged via PR #1 into the default branch, restarted it from the latest default branch before adding this session's commits, per the "merged PR is finished" rule.
- Verified all new `Store` methods directly against a temp SQLite file (promotion at the threshold, weekly gating, clearing after marking consulted) and the extractor fix against both a real single listing and a synthetic index page.

### Earlier sessions (compressed)

Reasoning that a Standing rule or Known bug still depends on has been moved into those sections; these lines are the sequence only.

- **blocked-sites list + local-run guide** — moved `pgfn` out of `perfis.ci` (it was timing out there for nothing), rewrote the stale "why two profiles" docs into a canonical blocked list, added a `.env`-missing warning to `run_local.sh`.
- **/init + /start** — installed the GitHub CLI, added `ruff.toml` (21 pre-existing findings, left unfixed), logged the /start standing rules. Declined a Black format-on-edit hook.
- **overnight /improve + /automate** — backup branch `backup/2026-08-11-pre-improve`; fixed Brave 422 (two causes), the curl_cffi/Caixa 200-with-CAPTCHA trigger, an unbounded Brave visit phase, stored XSS in the page template, Telegram HTML escaping; added `source_health` alerting; confirmed Pages was publishing.
- **Brave parallelization + persistent queue** — made `http.py`'s per-host throttle thread-safe, parallelized page visiting, added the `brave_pendentes` queue so a backlog carries across runs instead of being rediscovered and abandoned. One self-inflicted false alarm here produced the standing rule about never judging run duration from sandbox elapsed time.
- **Brave discover/visit split** — separated the API-limited discovery from the unlimited visiting, removed the artificial visit budget, raised parallelism to 50 with a 40s per-page timeout and cross-run retry-then-discard (`falhas`, 2 strikes). Verified live: 689/689 and 618/618 candidates cleared in one pass each.

