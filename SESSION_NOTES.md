# Terreno — Session Log

Living document. Append new sessions at the bottom under "## Session history". Keep the "Standing rules" and "Architecture / current state" sections up to date — they're what future sessions need to pick up work without re-litigating decisions.

> **Note on secrets**: this file intentionally contains **no raw API keys, passwords, or private keys**. Anything generated in a session (tokens, scratchpad files) lives in that session's ephemeral scratchpad and is **gone once the session ends**. Treat credential retrieval as "go get it from the dashboard again" in every new session, not "read it from disk."

---

## Standing rules (apply to every future session on this project)

- Ask a question once, then wait — no re-asking, no restating answers already given. (Also in CLAUDE.md, both global and project-level.)
- Claude can and should operate GitHub Actions directly (trigger runs, poll status, read logs via the `mcp__github__actions_*` / `get_job_logs` tools) instead of asking the user to download/paste logs.
- Repo `AtisMatiz/Terreno` is **public**, so GitHub Actions minutes are free/unmetered — job timeouts can be raised generously without a real cost tradeoff; the only reason to keep them bounded is as a backstop against a genuine hang.
- PGFN (`comprei.pgfn.gov.br`) stays out of automated CI for now by explicit user decision (2026-08-11) — see Known bugs below for why, and Pending for the deferred proxy option.
- When polling a long-running GitHub Actions run, use a real external timer (`Monitor` with `curl`, or a `Bash --run_in_background` loop hitting the GitHub API) — never judge run duration from this sandbox's own elapsed time / background-sleep completions, which do not reliably track real wall-clock time here. Misreading elapsed time as "stuck" previously caused a live, healthy run to be cancelled by mistake.

---

## Architecture / current state (as of 2026-08-11)

**What it does**: searches Brazilian portals + long-tail web/social for rural land for sale, filters/scores against `criteria.yaml`, publishes a sortable HTML page via GitHub Pages (`https://atismatiz.github.io/Terreno/`).

**Pipeline** (`terreno/run.py`): iterates `REGISTRY` sources (`terreno/sources/__init__.py`) enabled for the active profile (`ci` or `local`, in `criteria.yaml`), collects raw listings, normalizes/dedupes/filters/enriches/scores (`terreno/pipeline.py`), renders `site/index.html` (`terreno/render.py`), commits `data/terreno.sqlite3` + `site/` + `criteria.yaml` back to the branch, then a `deploy` job publishes to Pages.

**Sources**: `mercadolivre`, `pgfn`, `vivareal`, `olx`, `chavesnamao`/`imovelweb`/`wimoveis` (all via `htmlportal.py`), `caixa` (public CSV + `curl_cffi` for its bot wall), `brave` (long-tail via Brave Search API), `facebook`. CI profile only runs sources that actually work from GitHub's datacenter IP: `brave, chavesnamao, pgfn, caixa` (`local` profile runs everything, for when the user runs it from their own machine).

**Brave (Layer B)** is now two decoupled phases, each its own module:
- `terreno/sources/brave_discover.py` — queries the Brave API, queues every new candidate URL into the `brave_pendentes` SQLite table. Never fetches a page. Bound by Brave's real API limits: `brave_consultas_por_run`/`brave_consultas_por_mes` (free tier is 2000 queries/month).
- `terreno/sources/brave_visit.py` — loads the *entire* pending queue and visits it in parallel (`ThreadPoolExecutor`, `brave_paralelismo: 50`), no per-run time or count cap. Per-page timeout `brave_timeout_pagina_s: 40`. Three outcomes per candidate: extracted a listing → done; fetched but nothing there → done (discarded, revisiting won't help); couldn't fetch at all → `falhas` counter +1 in the queue, retried next run, discarded after `brave_max_falhas: 2` consecutive failures.
- `terreno/sources/brave.py` is now a thin orchestrator: `discover()` then `visit_all()`, same external `fetch()` signature the registry expects.
- The old shared 90s visit deadline is gone entirely — visiting was never actually API-limited (only discovery calls Brave), so the real backstop is the workflow job's own `timeout-minutes` (currently 90, in `.github/workflows/search.yml`), not an app-level budget.

**Database** (`terreno/store.py`, `data/terreno.sqlite3`, committed to the repo): `listings`, `price_history`, `geocache`, `budget_ledger`, `runs`, `source_health`, `brave_pendentes` (url, dica, descoberto_em, falhas). **Important**: `Store.__init__` runs `SCHEMA` via `CREATE TABLE IF NOT EXISTS` *and* an explicit `_migrate()` step — the schema-diff pattern needed any time a column is added to an existing table, since `IF NOT EXISTS` never alters a table that already exists and this DB is a committed file, not recreated per run.

**Infra**: GitHub Actions (`.github/workflows/search.yml`) — cron daily at 09:00 UTC + manual `workflow_dispatch` (accepts `profile`, `overrides`, `salvar` inputs). GitHub Pages serves the static site. **Vercel** (`api/disparar.js`) exists only to hold the GitHub PAT server-side for a "run search now" button on the page — GitHub Pages can't hold secrets since it's static-only. User has Vercel "already configured" on their end but branch/env-var wiring for the button isn't confirmed done yet (see Pending).

**Search criteria** are still narrow test values: SP, Vale do Paraíba, 2–10 ha, R$100k–2M — the simplest lever to get more results is widening these.

---

## Known bugs & fixes (reference for anyone touching this again)

- **Brave HTTP 422**: two separate causes, both fixed. (1) `country` param must be uppercase (`"BR"`, not `"br"`). (2) `search_lang: "pt"` fails Brave's enum validation (wants a full locale like `pt-BR`) — dropped entirely since the query text is already Portuguese.
- **curl_cffi never actually exercised against Caixa**: `http.py`'s automatic curl_cffi fallback only triggers on HTTP 403/429, but Caixa's Radware bot wall returns HTTP 200 with a CAPTCHA page — so `caixa.py` calls `http._via_cffi` directly in its own retry chain instead of relying on the generic fallback. Also `curl_cffi` was commented out in `requirements.txt` until promoted to a real dependency — check it's actually installed if this regresses.
- **PGFN (`comprei.pgfn.gov.br`) is blocked from CI by network-level timeout, not TLS fingerprinting** — confirmed repeatedly (`ConnectTimeoutError`/`SSLZeroReturnError`, not 403/429), which is why `curl_cffi` does *not* help here. It works fine from a residential connection (`--profile local`). A free fix would need a residential-IP proxy/tunnel from a home machine (Cloudflare Tunnel / Tailscale Funnel) — deferred, see Pending.
- **Stored XSS in `terreno/templates/page.html`**: every scraped field (including public Facebook search results — untrusted third-party content) was inserted via `innerHTML` with no escaping. Fixed with `escHtml()` (text) and `safeUrl()` (href/src, only allows http/https else collapses to `#`). Verified against a real malicious payload run through the actual pipeline.
- **Telegram notify HTML-escaping**: listing titles/URLs interpolated raw into a Telegram HTML-parse-mode message could contain a stray `<`/`&` and silently kill the whole notification. Fixed with a `_esc()` helper in `notify.py`.
- **Health-tracking must stay after `--dry-run`'s early return** in `run.py` — a preview run must never write to `source_health`, or it corrupts the real alert streak.
- **Git commit message + backticks inside double-quoted `-m "..."` in bash**: shell command substitution silently eats the backticked text. Cosmetic-only when it happened once; use single-quoted heredocs for commit messages with backticks/code references.
- **This sandbox's background-sleep timers don't reliably track real wall-clock time** — caused a real false "the run is deadlocked" alarm and a live healthy run got cancelled by mistake. Always poll external state (GitHub API) directly via `Monitor`/`curl`, never infer stuck-ness from elapsed sandbox time.

---

## Pending / next steps

1. **Facebook source**: user chose options 1+2+5 (indexed search via Brave + Apify without cookies + more sources) over a burner account — decided but **not yet implemented**. Next product/technical step.
2. **Vercel wiring**: user says Vercel is "already configured" but the production branch + env vars for the "run search now" button aren't confirmed working end-to-end.
3. **Widen search criteria** in `criteria.yaml` — still at narrow test values (SP/Vale do Paraíba, 2–10 ha) which is why real match counts stay low (2-9 per run) despite hundreds of Brave candidates.
4. **PGFN residential-IP tunnel** — deferred by explicit user decision on 2026-08-11 ("Vamos deixar o PGFN por enquanto"). If revisited: Cloudflare Tunnel or Tailscale Funnel from a home machine, routing only the `comprei.pgfn.gov.br` host through it via `requests`' `proxies=`.
5. **Watch the next few scheduled runs' `brave_pendentes` failure counts** — the cross-run retry-then-discard logic (`falhas`, threshold 2) was verified in a real run (171 fetch failures queued for retry, 0 discarded yet since none had failed twice) but a full discard cycle hasn't been observed live yet.
6. The broader "delegate cheap subtasks to Haiku subagents + automated fix/test/merge loop" architecture idea raised by the user was discussed and assessed (recommended doing concrete fixes first) but **not built** — still an open idea if the user wants to revisit it.

---

## Session history

### 2026-08-11 (overnight — /improve + /automate)
- Created backup branch `backup/2026-08-11-pre-improve` before any changes.
- Found and fixed via real production logs: Brave 422 (two causes), curl_cffi/Caixa 200-with-CAPTCHA trigger bug + missing real dependency, unbounded Brave visit phase (added a since-removed 90s budget + job `timeout-minutes: 20`), Wimoveis/PGFN wasting the visit budget (`SKIP_HOSTS`).
- Fixed stored-XSS in the page template and a Telegram HTML-escaping bug.
- Added `source_health` tracking + Telegram alert after 3 consecutive silent/blocked runs for a source.
- Confirmed GitHub Pages was live and actually publishing (`https://atismatiz.github.io/Terreno/`).
- Delivered a 1-page Portuguese summary of the night's work to the user.

### 2026-08-11 (Brave parallelization + persistent queue)
- User asked what Brave does and to fix it discovering ~800 candidates but only having time to visit ~30.
- Made `http.py`'s per-host throttle thread-safe (atomic slot reservation under a lock, sleep outside it).
- Parallelized Brave's page-visiting with `ThreadPoolExecutor`, added a persistent `brave_pendentes` SQLite queue so an unvisited backlog carries over between runs instead of being rediscovered and abandoned every time.
- Validated on real GitHub Actions runs (with one self-corrected false alarm: mistook a healthy in-progress run for a deadlock due to this sandbox's unreliable elapsed-time tracking, cancelled it, then found via logs it had actually completed correctly — disclosed to the user as its own mistake, not a code bug).
- Explained Vercel's purpose (holding the GitHub PAT server-side, since Pages can't hold secrets) as a mid-task aside.

### 2026-08-11 (Brave discover/visit split, uncapped visiting, higher parallelism, retry-then-discard)
- User: stop rationing Brave's visit phase across runs — split into a discovery program and a visiting program, and let the visiting program read *all* candidates found regardless of count/time, since visiting isn't API-limited (only Brave's search queries are).
- Split `brave.py` into `brave_discover.py` (queries Brave, queues URLs, bound by the real API quota) and `brave_visit.py` (loads and visits the *entire* queue, no per-run cap), with `brave.py` left as a thin orchestrator. Removed the now-dead `max_paginas_novas`/`brave_segundos_max_visita` config; raised the workflow's `timeout-minutes` 20→90 as the real (and free, since the repo is public) backstop.
- Verified live: a run visited 689/689 queued candidates in one pass (~3 min), clearing the entire backlog that had built up under the old capped design.
- Follow-up ask: raise parallelism to 50 (confirmed no real "power limit" — visiting is I/O-bound, thread overhead is trivial), set a generous 40s per-page timeout as the "failed to access" threshold, and give fetch failures a second chance across runs (2-strikes discard) instead of dropping them on the first bad connection — distinct from a page that loads fine but has no listing, which is still discarded immediately.
- Implemented: `brave_pendentes` gained a `falhas` column (with an explicit `ALTER TABLE` migration, since the DB file is committed and `CREATE TABLE IF NOT EXISTS` never alters an existing table), `brave_visit.py` now categorizes every candidate into success/no-content/fetch-failure and calls a new `Store.brave_pendentes_registrar_falha()` for the failure path.
- Verified live: `brave_paralelismo: 50`, `40s por página` confirmed active in logs; 618/618 candidates visited in ~90s (vs ~178s for a similar batch at parallelism 15); 171 fetch failures correctly queued for one retry (0 discarded, since none had failed twice yet).
- Also answered: PGFN can't be fixed with `curl_cffi` (it's a network-level timeout, not TLS fingerprinting) — a free fix would need a residential-IP tunnel from a home machine; user deferred this for now.
