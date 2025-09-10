#!/usr/bin/env bash
set -euo pipefail
echo "[update.sh] Pulling latest..."
if command -v git >/dev/null 2>&1; then
  git pull --ff-only || true
else
  echo "git not found; skipping auto-update."
fi

if [ ! -d .venv ]; then
  if command -v python3.10 >/dev/null 2>&1; then
    python3.10 -m venv .venv
  else
    python3 -m venv .venv
  fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py --server.address 0.0.0.0
