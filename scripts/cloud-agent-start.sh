#!/usr/bin/env bash
set -euo pipefail

cd /workspace

repair_venv_python() {
  local candidate=""
  for candidate in venv/bin/python3.* venv/bin/python3 venv/bin/python; do
    if [[ -x "$candidate" ]]; then
      ln -sf "$(basename "$candidate")" venv/bin/python
      ln -sf "$(basename "$candidate")" venv/bin/python3
      return 0
    fi
  done
  return 1
}

if [[ ! -d venv ]]; then
  echo "Virtual environment missing. Run ./scripts/cloud-agent-install.sh first." >&2
  exit 1
fi

repair_venv_python || {
  echo "Virtual environment is missing an executable Python interpreter." >&2
  exit 1
}

if [[ ! -f scrip_master.json ]]; then
  echo "scrip_master.json missing. Run ./venv/bin/python instrument_engine.py to refresh." >&2
  exit 1
fi

./venv/bin/python -c "from database import DatabaseManager; DatabaseManager()"
echo "Environment ready: venv, scrip master, and trade database initialized."
