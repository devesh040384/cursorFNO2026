#!/usr/bin/env bash
set -euo pipefail

cd /workspace

if ! python3 -m venv /tmp/_venv_probe 2>/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi
rm -rf /tmp/_venv_probe

if [[ ! -d venv ]] || [[ ! -x venv/bin/python ]]; then
  python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Ensure the trade ledger schema exists without starting the bot.
./venv/bin/python -c "from database import DatabaseManager; DatabaseManager()"
