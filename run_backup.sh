#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

mkdir -p logs
.venv/bin/python3 slack_backup.py >> "logs/$(date +%Y-%m-%d).log" 2>&1
