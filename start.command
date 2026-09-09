#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec bash tools/product-profit-monitor/scripts/start.sh
