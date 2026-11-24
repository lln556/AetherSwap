#!/usr/bin/env bash
# 启动 AetherSwap 自主交易服务（含 postgres）
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose -f docker-compose.yml -f compose.override.yaml -f compose.postgres.yaml up -d
