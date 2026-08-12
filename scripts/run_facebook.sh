#!/usr/bin/env bash
# Facebook cookies fallback only — a deliberately manual, low-cadence escape
# hatch, kept separate from CI because it is the one source that can get an
# account locked. Facebook's primary path (Apify) runs unattended in CI; this
# script is for the Playwright/cookies backup, which needs a local browser
# session and has no CI equivalent.
#
# Setup:
#   1. Log in to Facebook as the BURNER account in a browser.
#   2. Export cookies as JSON (e.g. the "Cookie-Editor" extension → Export JSON).
#   3. Save to ./fb_cookies.json and set FB_COOKIES_FILE in .env.
#   4. pip install playwright && playwright install chromium
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a

if [ -z "${FB_COOKIES_FILE:-}" ] || [ ! -f "${FB_COOKIES_FILE}" ]; then
  echo "FB_COOKIES_FILE not set or missing — see the header of this script." >&2
  exit 1
fi

python3 -m terreno.run --only facebook "$@"
