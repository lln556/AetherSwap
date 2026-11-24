#!/usr/bin/env bash
# 停止 AetherSwap 自主交易服务
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose -f docker-compose.yml -f compose.override.yaml -f compose.postgres.yaml down
