#!/usr/bin/env bash
set -euo pipefail

cd /workspace

if [[ ! -x venv/bin/python ]]; then
  echo "Virtual environment missing. Run ./scripts/cloud-agent-install.sh first." >&2
  exit 1
fi

if [[ ! -f scrip_master.json ]]; then
  echo "scrip_master.json missing. Run ./venv/bin/python instrument_engine.py to refresh." >&2
  exit 1
fi

./venv/bin/python -c "from database import DatabaseManager; DatabaseManager()"
echo "Environment ready: venv, scrip master, and trade database initialized."
