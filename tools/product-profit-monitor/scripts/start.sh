#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  candidate="${PYTHON_BIN:-python3}"
  "$candidate" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
  "$candidate" -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
mkdir -p .runtime
export PROFIT_STATE_DB="${PROFIT_STATE_DB:-.runtime/monitor.sqlite3}"
.venv/bin/python -m bi_check_agent.worker > .runtime/worker.log 2>&1 &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
printf 'Product Profit: http://127.0.0.1:8501\nKeep this terminal open. Ctrl+C stops the app and worker.\n'
.venv/bin/python -m streamlit run app.py --server.headless true --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
