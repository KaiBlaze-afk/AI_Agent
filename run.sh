#!/usr/bin/env bash
# ==============================================================================
# Aria // Venice Agent Workspace - Launch Script
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d ".venv" ]]; then
    echo "[!] Virtual environment not found. Running setup first..."
    ./setup.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "======================================================"
echo " Starting Aria Venice Agent Workspace..."
echo "======================================================"

python app.py

