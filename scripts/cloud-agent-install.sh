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

if ! python3 -m venv /tmp/_venv_probe 2>/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi
rm -rf /tmp/_venv_probe

rm -rf venv
python3 -m venv --copies venv
repair_venv_python

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

repair_venv_python

# Ensure the trade ledger schema exists without starting the bot.
./venv/bin/python -c "from database import DatabaseManager; DatabaseManager()"
