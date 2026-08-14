#!/usr/bin/env bash
# Collect a past range into data/scan/. Local tool: no email, no summaries.
#
#   scripts/scan.sh --days 365          the last year
#   scripts/scan.sh --last 30           the newest 30 articles
#   scripts/scan.sh --since 2026-01-01  everything since a date
#   scripts/scan.sh --no-crawl --days 7 what is already stored, requesting nothing
#
# Finds the project's virtualenv so the command works from any directory and
# without activating anything first.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x "${repo_root}/.venv/bin/python" ]]; then
  python="${repo_root}/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
  python="${VIRTUAL_ENV}/bin/python"
else
  echo "No virtualenv found at ${repo_root}/.venv and none is active." >&2
  echo "Create one with:  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

exec "${python}" -m it_newsletter.cli.scan "$@"
