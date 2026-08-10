#!/usr/bin/env bash
# Local run — the sources that only work from a residential IP.
#
# OLX, VivaReal/ZAP, Imovelweb and Mercado Livre all answer 403 to datacenter
# ranges, and GitHub Actions runners are datacenter ranges. Facebook is stricter
# still. So those sources run here, on your own connection, and push their
# results to the same repository the Action publishes from.
#
# Schedule it with cron, e.g. daily at 07:00:
#   0 7 * * * cd /path/to/Terreno && ./scripts/run_local.sh >> /tmp/terreno.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a

python3 -m terreno.run --profile local "$@"

if ! git diff --quiet -- data/terreno.sqlite3 site/; then
  git add data/terreno.sqlite3 site/
  git commit -m "chore: local search results $(date -u +%Y-%m-%dT%H:%MZ)"
  for i in 1 2 3 4; do
    git pull --rebase --autostash && git push && break
    sleep $((2 ** i))
  done
fi
