#!/usr/bin/env bash
# Local run — the sources that only work from a residential IP: olx, vivareal,
# mercadolivre, imovelweb, wimoveis and pgfn answer 403 or drop the connection
# to datacenter ranges, and GitHub Actions runners are datacenter ranges.
# Facebook is stricter still. So those sources run here, on your own
# connection, and push their results to the same repository the Action
# publishes from. Canonical list: `perfis.local` in criteria.yaml, see the
# README's "Blocked sites" section for the full explanation.
#
# Schedule it with cron, e.g. daily at 07:00:
#   0 7 * * * cd /path/to/Terreno && ./scripts/run_local.sh >> /tmp/terreno.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "warning: no .env found — running with whatever keys are already in the" \
       "environment (pgfn/facebook/olx/etc. don't need one, but Brave and" \
       "Facebook via Apify won't do anything without theirs). Copy .env.example" \
       "to .env and fill it in if that's not what you want." >&2
else
  set -a && . ./.env && set +a
fi

python3 -m terreno.run --profile local "$@"

if ! git diff --quiet -- data/terreno.sqlite3 site/; then
  git add data/terreno.sqlite3 site/
  git commit -m "chore: local search results $(date -u +%Y-%m-%dT%H:%MZ)"
  for i in 1 2 3 4; do
    git pull --rebase --autostash && git push && break
    sleep $((2 ** i))
  done
fi
